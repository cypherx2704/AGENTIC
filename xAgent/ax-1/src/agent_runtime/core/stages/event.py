"""EVENT stage — the finally-equivalent terminal stage (Component 3b).

Runs LAST on EVERY path (success, short-circuit, or an upstream exception that the
runner converted to a terminal error). It does two durable writes:

  1. Persists any buffered audit steps NOT already written-through. With per-stage
     write-through (WP08) each user-visible stage persists its own ``task_steps`` row as
     it completes (so ``GET /v1/tasks/{id}`` shows ordered steps mid-run); EVENT is now a
     BACKSTOP that only (re)inserts rows whose write-through was skipped (no pool yet) or
     failed (``StepRow.persisted == False``), so a step is never double-inserted and a
     missed write-through self-heals at finalisation.
  2. Finalises the task row + emits exactly one terminal Kafka event ATOMICALLY via
     ``outbox.record_task_event`` (the UPDATE + outbox INSERT share one tenant tx, so
     the row and the event can never diverge). Success -> ``cypherx.agent.task.completed``
     with ``output={'message': final_answer}``; otherwise -> ``cypherx.agent.task.failed``
     with the ``error_message`` payload field (FIX 1; the column is ``error_msg``).

The terminal status is taken from ``ctx.terminal_error`` (failed | timeout | cancelled)
or ``completed`` when none is set. ``duration_ms`` is wall-clock from
``ctx.started_monotonic``. The runner wraps this stage so any exception here is logged,
never raised — the api layer still returns a response (the outbox keeps events durable).
"""

from __future__ import annotations

import time

import structlog

from ...db import outbox
from ...db.steps_repo import insert_task_step
from .. import metrics
from ..config import get_settings
from ..pipeline import PipelineContext, Stage

logger = structlog.get_logger(__name__)


class EventStage(Stage):
    """Persist audit steps + finalise the task and emit its terminal event (atomic).

    The SINGLE authoritative EVENT stage. EVENT is not a STAGE_REGISTRY slot — the api layer
    constructs one instance (with the service version) and passes it to ``Pipeline.from_registry``.
    """

    name = "EVENT"

    def __init__(self, *, producer_version: str | None = None) -> None:
        # Injected by the api layer (settings.service_version); falls back to settings if unset.
        self._producer_version = producer_version

    async def run(self, ctx: PipelineContext) -> None:
        pool = ctx.pool
        if pool is None:  # EVENT must never raise; without a pool there is nothing to write.
            metrics.event_write_failed_total.labels("no_pool").inc()
            logger.warning("event_write_skipped_no_pool", task_id=ctx.task.task_id)
            return

        # 1) BACKSTOP-persist any audit steps the per-stage write-through did not land
        # (no pool at the time, or a transient INSERT error). Already-persisted rows are
        # skipped so a step is never double-inserted. Best-effort, RLS-scoped, fail-soft.
        if ctx.steps is not None:
            for step in ctx.steps.steps:
                if step.persisted:
                    continue
                try:
                    await insert_task_step(pool, step)
                    step.persisted = True
                except Exception as exc:  # noqa: BLE001 — a step write must not abort finalisation
                    logger.warning(
                        "task_step_persist_failed",
                        task_id=ctx.task.task_id,
                        step_name=step.step_name,
                        error=str(exc),
                    )

        # 2) Finalise the task row + emit the terminal Kafka event atomically.
        duration_ms = int((time.monotonic() - ctx.started_monotonic) * 1000)
        term = ctx.terminal_error

        if term is None:
            status = "completed"
            output: dict[str, str] | None = {"message": ctx.final_answer or ""}
            error_code = None
            error_message = None
        else:
            status = term.status or "failed"  # failed | timeout | cancelled (default closed)
            output = None
            error_code = term.code
            error_message = term.message

        write = outbox.TaskEventWrite(
            task_id=ctx.task.task_id,
            tenant_id=ctx.task.tenant_id,
            agent_id=ctx.task.agent_id,
            trace_id=ctx.trace_id,
            status=status,
            tokens_used=ctx.tokens_used,
            cost_usd=ctx.cost_usd,
            duration_ms=duration_ms,
            output=output,
            error_code=error_code,
            error_message=error_message,  # FIX 1 — payload field name (col is error_msg)
        )
        await outbox.record_task_event(
            pool,
            write,
            producer_version=self._producer_version or get_settings().service_version,
        )
        metrics.tasks_total.labels(status).inc()
        metrics.task_duration_seconds.labels(status).observe(duration_ms / 1000)
