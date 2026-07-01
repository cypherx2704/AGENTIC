"""Lifespan-scheduled DB maintenance jobs (WP07 — Ops).

Currently one job: the **outbox purge**. The transactional outbox accumulates a row per
published Kafka event; once ``published_at`` is set the row is durable history we no
longer need. The purge deletes published rows older than ``outbox_retention_hours`` on a
loop (``outbox_purge_interval_seconds``), keeping the ``idx_outbox_unpublished`` working
set small and the table bounded.

Safety:
  * Only rows with ``published_at IS NOT NULL`` are eligible — UNPUBLISHED rows (still
    pending delivery, or DLQ-bound) are NEVER purged.
  * The publisher marks DLQ'd rows ``published_at = NOW()`` after forwarding, so they are
    swept on the same retention schedule.
  * Fail-soft: a DB error logs + retries next tick; the loop never crashes the service.
  * RLS: outbox RLS is intentionally disabled (internal cross-tenant queue), so the purge
    runs without an ``app.tenant_id`` — same posture as the publisher's drain.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import metrics

logger = structlog.get_logger(__name__)


class OutboxPurger:
    """Background task that deletes published outbox rows past the retention window."""

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        retention_hours: int,
        interval_seconds: float,
        enabled: bool = True,
    ) -> None:
        self._pool = pool
        self._retention_hours = retention_hours
        self._interval = interval_seconds
        self._enabled = enabled
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._pool is None or not self._enabled:
            return
        self._task = asyncio.create_task(self._run(), name="outbox-purger")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            if self._stopping.is_set():
                return
            try:
                await self.purge_once()
            except Exception as exc:  # noqa: BLE001 — the loop must keep running
                logger.warning("outbox_purge_error", error=str(exc))

    async def purge_once(self) -> int:
        """Delete eligible published rows; return the count deleted (0 if no pool)."""
        if self._pool is None:
            return 0
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                DELETE FROM guardrails.outbox
                 WHERE published_at IS NOT NULL
                   AND published_at < NOW() - make_interval(hours => %s)
                """,
                (self._retention_hours,),
            )
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if deleted:
            metrics.outbox_purged_total.inc(deleted)
            logger.info("outbox_purged", deleted=deleted, retention_hours=self._retention_hours)
        return deleted
