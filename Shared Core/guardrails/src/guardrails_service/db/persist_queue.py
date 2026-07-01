"""Post-response persistence queue (WP07 — Component 4 amendment).

The hot check path computes the security decision and returns immediately; the
violation-row + usage/outbox writes (which used to run inline, fail-soft, before the
response) are now ENQUEUED here and drained by a single background worker task. This
keeps the DB write off the request's critical path while preserving correctness.

Semantics (documented):
  * **Overflow** — the queue is bounded (``persist_queue_maxsize``). If it is full when a
    check enqueues, the item is DROPPED with a WARN log + ``persist_queue_dropped_total``
    counter. The security decision is already returned to the caller, so a drop degrades
    metering/audit completeness, never safety. (We drop the *new* item rather than block
    the response — back-pressure on a safety check is worse than a lost audit row.)
  * **Backlog** — ``persist_queue_backlog`` gauge tracks the live depth for alerting.
  * **Shutdown** — :meth:`stop` signals the worker and waits up to
    ``persist_queue_drain_timeout_seconds`` for the backlog to flush before cancelling, so
    a graceful shutdown does not silently lose buffered writes.
  * **No DB pool** — if no pool is wired (unit/local) the queue is never started and
    :meth:`enqueue` is a no-op, matching the previous inline "skip persistence" behaviour.

Each write is still individually fail-soft: a DB error on one item logs + counts and the
worker moves on (one bad write never stalls the queue).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from .outbox import CheckWrite, record_check

logger = structlog.get_logger(__name__)


class PersistenceQueue:
    """In-process async queue draining CheckWrite items to the DB in the background."""

    def __init__(
        self,
        pool_getter: Callable[[], AsyncConnectionPool | None],
        *,
        producer_version: str,
        maxsize: int = 10_000,
        drain_timeout_seconds: float = 5.0,
    ) -> None:
        # A GETTER (not a captured pool) so a pool opened/swapped after start() — e.g. a
        # lazily-opened prod pool, or a test injecting app.state.db_pool post-startup — is
        # honoured at enqueue/drain time, matching the previous inline "read pool per request".
        self._pool_getter = pool_getter
        self._producer_version = producer_version
        self._drain_timeout = drain_timeout_seconds
        self._queue: asyncio.Queue[CheckWrite] = asyncio.Queue(maxsize=max(maxsize, 1))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._pool_getter() is not None

    async def start(self) -> None:
        """Start the background drain worker. The worker idles until a pool + items exist."""
        self._task = asyncio.create_task(self._run(), name="persist-queue-worker")

    def enqueue(self, write: CheckWrite) -> None:
        """Enqueue a check's persistence work. Non-blocking; drops on overflow.

        Synchronous (no await) so the request handler never yields on the queue. When no
        DB pool is currently configured this is a no-op (local/unit path).
        """
        if self._pool_getter() is None:
            return
        try:
            self._queue.put_nowait(write)
        except asyncio.QueueFull:
            metrics.persist_queue_dropped_total.inc()
            logger.warning(
                "persist_queue_overflow",
                tenant_id=write.tenant_id,
                direction=write.direction,
                decision=write.decision,
            )
            return
        metrics.persist_queue_backlog.set(self._queue.qsize())

    async def stop(self) -> None:
        """Signal shutdown, drain the backlog (best-effort, bounded), then cancel."""
        self._stopping.set()
        if self._task is None:
            return
        # Only wait on join() if there is anything to flush (the worker may have been idle).
        # Give the worker a bounded chance to flush what's queued.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
            await self._task

    async def _run(self) -> None:
        while True:
            try:
                write = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                if self._stopping.is_set() and self._queue.empty():
                    return
                continue
            try:
                await self._persist_one(write)
            finally:
                self._queue.task_done()
                metrics.persist_queue_backlog.set(self._queue.qsize())

    async def _persist_one(self, write: CheckWrite) -> None:
        pool = self._pool_getter()
        if pool is None:
            return  # pool went away between enqueue and drain — skip (metering loss, not safety)
        try:
            await record_check(pool, write, producer_version=self._producer_version)
            metrics.persist_queue_processed_total.labels("ok").inc()
        except Exception as exc:  # noqa: BLE001 — one bad write must not stall the queue
            metrics.persist_queue_processed_total.labels("failed").inc()
            metrics.violation_write_failed_total.labels("db_unreachable").inc()
            logger.error("violation_write_failed", reason="db_unreachable", error=str(exc))
