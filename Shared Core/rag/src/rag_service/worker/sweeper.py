"""S3-deletion sweeper (Component 5) — drains rag.s3_deletions.

Document delete writes a durable ``rag.s3_deletions`` row inside the DB-delete txn (so a
transient S3 outage never blocks the user's erasure request). This background loop every
``s3_deletion_sweep_interval_seconds`` reads a batch, calls ``DeleteObjects`` under each
prefix, and DELETEs the row on success (leaving it for retry on failure). It is a
platform-internal table (no RLS) drained across all tenants.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.config import Settings
from ..services.object_store import ObjectStore

logger = structlog.get_logger(__name__)


class S3DeletionSweeper:
    def __init__(
        self, pool: AsyncConnectionPool, object_store: ObjectStore, settings: Settings
    ) -> None:
        self._pool = pool
        self._store = object_store
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="s3-deletion-sweeper")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.sweep_once()
            except Exception as exc:  # noqa: BLE001 — sweeper must keep running
                logger.warning("s3_deletion_sweep_error", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._settings.s3_deletion_sweep_interval_seconds,
                )

    async def sweep_once(self) -> int:
        """Process one batch of pending deletions; returns the count deleted."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT doc_id, s3_prefix FROM rag.s3_deletions "
                "WHERE attempts < 100 ORDER BY requested_at LIMIT %s",
                (self._settings.s3_deletion_batch_size,),
            )
            rows = await cur.fetchall()

        metrics.s3_deletions_pending.set(len(rows))
        deleted = 0
        for row in rows:
            ok = await self._store.delete_prefix(row["s3_prefix"])
            async with self._pool.connection() as conn:
                if ok:
                    await conn.execute(
                        "DELETE FROM rag.s3_deletions WHERE doc_id = %s", (row["doc_id"],)
                    )
                    deleted += 1
                else:
                    await conn.execute(
                        "UPDATE rag.s3_deletions SET attempts = attempts + 1 WHERE doc_id = %s",
                        (row["doc_id"],),
                    )
        return deleted
