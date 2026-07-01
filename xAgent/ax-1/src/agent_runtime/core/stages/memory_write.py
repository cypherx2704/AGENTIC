"""MEMORY_WRITE stage — persist an interaction memory after the answer (Component 6, WP12).

Runs LATE in the pipeline (registry slot after POST_GUARDRAIL, before EVENT) so it records
the FINAL, guardrail-checked answer. TRIGGER: registry-disabled by default; even when
``STAGE_ENABLE_MEMORY_WRITE`` is on, the stage SKIPS unless ALL of:
  * the agent's ``memory_scope`` is not ``none`` (memory enabled for the agent),
  * ``settings.memory_write_enabled`` is on (the global write toggle),
  * the task did NOT short-circuit before producing an answer (``ctx.final_answer`` set and
    no terminal error) — we never store a memory of a failed/blocked interaction.

BEHAVIOUR: store a single memory of the interaction (the user message + the final answer),
scoped by ``memory_scope`` + ``session_id``, typed ``settings.memory_write_type``. FAIL
-SOFT: a store error is logged and swallowed — a memory-write blip never fails an
otherwise-successful task (the answer was already produced + guardrail-checked).

STEP: writes ONE ``memory_write`` audit step. Per-stage write-through.
"""

from __future__ import annotations

import time

import structlog

from ...db import steps_repo
from ...db.steps_repo import StepRow
from ...models.task import STEP_TYPE_MEMORY_WRITE
from ..config import get_settings
from ..errors import ApiError
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)

# Cap the stored interaction content so a huge answer never bloats the memory store. Not a
# tunable behaviour knob — a defensive bound on a side-channel write.
_MAX_STORED_CHARS = 8000


class MemoryWriteStage(Stage):
    """Store a memory of the (successful) interaction; scoped by memory_scope + session_id."""

    name = "MEMORY_WRITE"

    async def run(self, ctx: PipelineContext) -> None:
        agent = ctx.agent
        settings = get_settings()
        if agent is None or agent.memory_scope == "none" or not settings.memory_write_enabled:
            return  # memory write disabled for this agent / globally -> no-op
        if ctx.terminal_error is not None or not ctx.final_answer:
            return  # never store a memory of a failed / blocked / answer-less interaction

        scope = agent.memory_scope
        session_id = ctx.session_id if scope == "session" else None
        if scope == "session" and not session_id:
            return  # session-scoped but no correlator — nothing to scope the memory to

        user_message = ""
        if isinstance(ctx.task.input, dict):
            msg = ctx.task.input.get("message")
            user_message = msg if isinstance(msg, str) else ""
        content = f"User: {user_message}\nAssistant: {ctx.final_answer}"[:_MAX_STORED_CHARS]

        started = time.monotonic()
        status = "passed"
        stored_id: str | None = None
        try:
            result = await deps.get_memory_client().store(
                content,
                agent_jwt=ctx.inbound_agent_jwt,
                type=settings.memory_write_type,
                scope=scope,
                session_id=session_id,
                metadata={"task_id": ctx.task.task_id},
                on_behalf_of=ctx.principal.agent_id,
            )
            stored_id = result.id
        except ApiError as exc:
            # FAIL-SOFT: a memory-write blip never fails an otherwise-successful task.
            status = "failed"
            logger.warning("memory_write_failed", task_id=ctx.task.task_id, error=exc.message)

        duration_ms = int((time.monotonic() - started) * 1000)
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_MEMORY_WRITE,
                step_name="memory_write",
                status=status,
                duration_ms=duration_ms,
                output={"memory_id": stored_id, "scope": scope, "stored": stored_id is not None},
            ),
        )
