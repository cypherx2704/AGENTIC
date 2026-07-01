"""Repository functions for ``xagent.task_steps`` (RLS-scoped audit trail, Component 6).

Exactly one row is written per user-visible pipeline stage at stage completion. The
first-cycle pipeline writes EXACTLY THREE rows per successful task (Contract 15 #7):
``guardrail_check_input``, ``llm_call``, ``guardrail_check_output`` — in that order.

PER-STAGE WRITE-THROUGH (WP08): each stage persists its row AS IT COMPLETES (via
:func:`record_step` -> ``ctx.steps.add`` + an immediate fail-soft INSERT) rather than
buffering every row for a single post-hoc flush in the EVENT stage. This makes
``GET /v1/tasks/{id}`` show ordered steps even MID-RUN. A step-write failure is logged
and swallowed — it sets ``StepRow.persisted = False`` so the EVENT stage re-attempts the
INSERT at finalisation (a missed write-through self-heals); it NEVER fails the task.

Status values use the INTERNAL enum (running | passed | failed | timeout | redacted).
``redacted`` is kept ONLY here in the audit row; the A2A response maps it to ``passed``
(see ``models/a2a.py`` FIX 2). The guardrails-decision -> row-status mapping is:

    allow | warn -> passed ; redact -> redacted ; block -> failed
    llm success  -> passed ; provider error -> failed ; timeout -> timeout
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .pool import in_tenant

logger = structlog.get_logger(__name__)

# Internal step status enum (CHECK constraint on the table).
STEP_STATUSES = frozenset({"running", "passed", "failed", "timeout", "redacted"})


@dataclass
class StepRow:
    """A finalised audit step, ready to persist (and to project into the A2A response)."""

    task_id: str
    tenant_id: str
    step_type: str  # guardrail_check | llm_call | tool_call | memory_* | skill_load
    step_name: str  # guardrail_check_input | llm_call | guardrail_check_output | ...
    status: str  # internal enum (may be 'redacted')
    duration_ms: int
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    tokens_used: int | None = None
    # True once the per-stage write-through INSERT has landed. The EVENT stage only
    # (re)persists rows still False, so a write-through is never double-inserted and a
    # failed write-through is retried at finalisation. Not a DB column.
    persisted: bool = False


async def insert_task_step(pool: AsyncConnectionPool, step: StepRow) -> None:
    """INSERT one audit row, stamping ``completed_at`` = NOW() for terminal steps."""

    async def _q(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            INSERT INTO xagent.task_steps
              (task_id, tenant_id, step_type, step_name, status, input, output,
               duration_ms, tokens_used, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                step.task_id,
                step.tenant_id,
                step.step_type,
                step.step_name,
                step.status,
                Jsonb(step.input) if step.input is not None else None,
                Jsonb(step.output) if step.output is not None else None,
                step.duration_ms,
                step.tokens_used,
            ),
        )

    await in_tenant(pool, step.tenant_id, _q)


async def record_step(
    pool: AsyncConnectionPool | None,
    buffer: StepBuffer | None,
    step: StepRow,
) -> None:
    """Per-stage WRITE-THROUGH: append ``step`` to ``buffer`` AND persist it immediately.

    Called by every user-visible stage at completion so ``GET /v1/tasks/{id}`` shows the
    ordered steps even mid-run. FAIL-SOFT: a missing pool (tests / no DB) skips the INSERT,
    and an INSERT error is logged + swallowed — the step stays in the buffer with
    ``persisted = False`` so the EVENT stage re-attempts it at finalisation. A step-write
    failure NEVER fails the task.

    The buffer remains the in-process source of truth the api layer projects into the A2A
    response, so the response is identical whether or not the write-through landed.
    """
    if buffer is not None:
        buffer.add(step)
    if pool is None:
        return  # no DB (tests / degraded) — buffer-only; EVENT no-ops the persist too.
    try:
        await insert_task_step(pool, step)
        step.persisted = True
    except Exception as exc:  # noqa: BLE001 — a step write must never fail the task
        logger.warning(
            "task_step_write_through_failed",
            task_id=step.task_id,
            step_name=step.step_name,
            error=str(exc),
        )


async def list_steps(pool: AsyncConnectionPool, tenant_id: str, task_id: str) -> list[StepRow]:
    """Return all audit rows for a task in creation order (feeds the A2A response)."""

    async def _q(conn: AsyncConnection) -> list[StepRow]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT task_id::text AS task_id, tenant_id::text AS tenant_id, step_type, step_name,
                   status, input, output, duration_ms, tokens_used
              FROM xagent.task_steps
             WHERE task_id = %s
             ORDER BY created_at
            """,
            (task_id,),
        )
        rows = await cur.fetchall()
        return [
            StepRow(
                task_id=r["task_id"],
                tenant_id=r["tenant_id"],
                step_type=r["step_type"],
                step_name=r["step_name"],
                status=r["status"],
                duration_ms=r["duration_ms"] or 0,
                input=r.get("input"),
                output=r.get("output"),
                tokens_used=r.get("tokens_used"),
            )
            for r in rows
        ]

    return await in_tenant(pool, tenant_id, _q)


@dataclass
class StepBuffer:
    """In-memory accumulator for the steps emitted during one task execution.

    The pipeline appends a :class:`StepRow` per user-visible stage; the EVENT stage
    (or the api layer) both persists them and projects them into the A2A response, so
    the wire response and the audit trail are built from the SAME ordered list.
    """

    steps: list[StepRow] = field(default_factory=list)

    def add(self, step: StepRow) -> None:
        self.steps.append(step)
