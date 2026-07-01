"""MEMORY_RETRIEVE stage — pull relevant memories before PROMPT_BUILD (Component 6, WP12).

TRIGGER: registry-disabled by default; even when ``STAGE_ENABLE_MEMORY_RETRIEVE`` is on,
the stage SKIPS unless the agent's ``memory_scope`` is something other than ``none``. So an
agent with ``memory_scope='none'`` carries no memory behaviour regardless of the flag.

BEHAVIOUR: call ``MemoryClient.search(prompt_text, top_k)`` scoped by the agent's
``memory_scope`` and (for ``session`` scope) the task's ``session_id``. The retrieved
memories are stashed on ``ctx.memories`` for PROMPT_BUILD to splice + budget. FAIL-SOFT:
memory is an optional enhancement, so a transport/service error is logged and the stage
proceeds with no memories (the task is never failed by a memory blip). A ``session`` scope
with no ``session_id`` degrades to a no-op (there is no conversation to scope to).

STEP: writes ONE ``memory_retrieve`` audit step. Per-stage write-through.
"""

from __future__ import annotations

import time

import structlog

from ...db import steps_repo
from ...db.steps_repo import StepRow
from ...models.task import STEP_TYPE_MEMORY_RETRIEVE
from ..config import get_settings
from ..errors import ApiError
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)


class MemoryRetrieveStage(Stage):
    """Search relevant memories (scope incl. session_id); stash them for PROMPT_BUILD."""

    name = "MEMORY_RETRIEVE"

    async def run(self, ctx: PipelineContext) -> None:
        agent = ctx.agent
        if agent is None or agent.memory_scope == "none":
            return  # memory disabled for this agent -> no-op (default-disabled shape)

        scope = agent.memory_scope
        session_id = ctx.session_id if scope == "session" else None
        if scope == "session" and not session_id:
            return  # session-scoped but no session correlator — nothing to scope to

        started = time.monotonic()
        settings = get_settings()
        status = "passed"
        retrieved = 0
        try:
            result = await deps.get_memory_client().search(
                ctx.prompt_text,
                settings.memory_retrieve_top_k,
                agent_jwt=ctx.inbound_agent_jwt,
                scope=scope,
                session_id=session_id,
                on_behalf_of=ctx.principal.agent_id,
            )
        except ApiError as exc:
            # FAIL-SOFT: proceed with no memories (memory is an optional enhancement).
            status = "failed"
            logger.warning("memory_retrieve_failed", task_id=ctx.task.task_id, error=exc.message)
        else:
            for item in result.results:
                ctx.memories.append({"id": item.id, "content": item.content, "score": item.score})
            retrieved = len(result.results)

        duration_ms = int((time.monotonic() - started) * 1000)
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_MEMORY_RETRIEVE,
                step_name="memory_retrieve",
                status=status,
                duration_ms=duration_ms,
                output={"memories_retrieved": retrieved, "scope": scope},
            ),
        )
