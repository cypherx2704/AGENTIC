"""Backup task-lifecycle sweeper (WP08) — lifespan-scheduled, fail-soft.

The in-process per-task ``asyncio.timeout`` guard (api layer) is the PRIMARY timeout: it
finalises a task the moment its own coroutine overruns. But a worker that CRASHES mid-run
never executes that guard, leaving the task wedged in ``pending`` / ``running`` forever.
This background sweeper is the BACKSTOP: it periodically finds non-terminal tasks past
their deadline and finalises them ``failed`` AND inserts the ``task.failed`` outbox row
ATOMICALLY (same tenant transaction, via ``outbox.sweep_task_failed``), so a crashed task
still produces exactly one terminal event and the Task Feed never shows an eternal
``running``.

The sweeper ALSO owns RETENTION: it deletes published ``outbox`` rows older than
``outbox_retention_days`` and ``task_steps`` older than ``task_steps_retention_days``
(config-driven), keeping the hot tables bounded.

Design + safety:
  * DISCOVERY is cross-tenant. The runtime role is NOT BYPASSRLS, so discovery (and the
    task_steps retention delete) run inside a transaction that sets ``app.sweeper = 'on'``
    — the additive opt-in RLS policy from migration 0004 (OR-combined with tenant
    isolation). Normal task-path transactions never set it, so isolation is unchanged.
  * FINALIZE is per-tenant + GUARDED. Each stuck task is finalised in its OWN tenant
    transaction (``app.tenant_id`` set); the UPDATE is guarded to ``pending|running`` so
    a task the pipeline finalised first is never clobbered (no duplicate event).
  * FAIL-SOFT everywhere: a DB hiccup logs + is swallowed; the loop keeps running. A
    missing pool (tests: ``db_pool = None``) makes the loop a quiet no-op.
  * Lifespan-scheduled: ``start()`` from the api-layer lifespan, ``stop()`` on shutdown.
    Gated by ``SWEEPER_ENABLED`` (tests leave it OFF — no DB).
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.config import Settings
from ..db import outbox, tasks_repo

logger = structlog.get_logger(__name__)

# Error envelope the sweeper writes for a task it backstops (the in-process timeout never
# ran — most likely a crashed worker). A protocol constant, not per-deployment config.
_SWEEP_ERROR_CODE = "INTERNAL_ERROR"
_SWEEP_ERROR_MESSAGE = "Task exceeded its deadline and was failed by the backup sweeper."


class TaskSweeper:
    """Periodic backstop: finalise stuck tasks + run retention (fail-soft)."""

    def __init__(self, pool: AsyncConnectionPool | None, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Launch the background loop (no-op if already running)."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="task-sweeper")
        logger.info("task_sweeper_started", interval_s=self._settings.sweeper_interval_seconds)

    async def stop(self) -> None:
        """Signal the loop to stop and await its teardown (never raises)."""
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.sweep_once()
                metrics.sweeper_runs_total.labels("ok").inc()
            except Exception as exc:  # noqa: BLE001 — the loop must outlive any single failure
                metrics.sweeper_runs_total.labels("error").inc()
                logger.warning("task_sweeper_cycle_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.sweeper_interval_seconds
                )

    async def sweep_once(self) -> None:
        """One sweep cycle: finalise stuck tasks, then run retention. Fail-soft per step."""
        if self._pool is None:
            return  # no DB (tests) — quiet no-op
        await self._sweep_stuck_tasks()
        await self._run_retention()

    # ── stuck-task backstop ────────────────────────────────────────────────────────
    async def _sweep_stuck_tasks(self) -> None:
        stuck = await self._discover_stuck()
        for task in stuck:
            try:
                finalised = await outbox.sweep_task_failed(
                    self._pool,  # type: ignore[arg-type] — _pool is not None inside sweep_once
                    task_id=task.task_id,
                    tenant_id=task.tenant_id,
                    agent_id=task.agent_id,
                    trace_id=task.trace_id,
                    error_code=_SWEEP_ERROR_CODE,
                    error_message=_SWEEP_ERROR_MESSAGE,
                    producer_version=self._settings.service_version,
                )
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
                logger.warning("task_sweep_finalize_failed", task_id=task.task_id, error=str(exc))
                continue
            if finalised:
                metrics.sweeper_tasks_swept_total.inc()
                logger.info("task_swept_failed", task_id=task.task_id, tenant_id=task.tenant_id)

    async def _discover_stuck(self) -> list[tasks_repo.StuckTask]:
        """Cross-tenant discovery under the sweeper RLS bypass (read-only transaction)."""
        assert self._pool is not None
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.sweeper', 'on', true)")
            return await tasks_repo.list_stuck_tasks(
                conn,
                grace_seconds=self._settings.sweeper_stuck_grace_seconds,
                limit=self._settings.sweeper_batch_limit,
            )

    # ── retention ──────────────────────────────────────────────────────────────────
    async def _run_retention(self) -> None:
        """Prune published outbox rows + old task_steps (best-effort, fail-soft)."""
        assert self._pool is not None
        try:
            async with self._pool.connection() as conn, conn.transaction():
                # task_steps is RLS'd -> needs the sweeper bypass; outbox has no RLS but
                # running both deletes in one tx is fine (the GUC is harmless for outbox).
                await conn.execute("SELECT set_config('app.sweeper', 'on', true)")
                steps_deleted = await tasks_repo.delete_old_task_steps(
                    conn, retention_days=self._settings.task_steps_retention_days
                )
                outbox_deleted = await tasks_repo.delete_old_outbox(
                    conn, retention_days=self._settings.outbox_retention_days
                )
        except Exception as exc:  # noqa: BLE001 — retention is best-effort
            logger.warning("task_sweeper_retention_failed", error=str(exc))
            return
        if steps_deleted:
            metrics.sweeper_rows_deleted_total.labels("task_steps").inc(steps_deleted)
        if outbox_deleted:
            metrics.sweeper_rows_deleted_total.labels("outbox").inc(outbox_deleted)
        if steps_deleted or outbox_deleted:
            logger.info(
                "task_sweeper_retention",
                task_steps_deleted=steps_deleted,
                outbox_deleted=outbox_deleted,
            )
