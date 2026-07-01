"""RAG_QUERY stage — knowledge-base retrieval before PROMPT_BUILD (Component 5, WP12).

TRIGGER: the slot is registry-disabled by default; even when ``STAGE_ENABLE_RAG_QUERY``
is on, the stage SKIPS (no-op) unless the agent's runtime config lists ``allowed_kb_ids``.
So an agent with no KBs configured carries no RAG behaviour regardless of the flag.

BEHAVIOUR: for each allowed KB id, call ``RagClient.query(kb_id, prompt_text, top_k)``
with ``top_k = min(agent.rag_top_k_per_kb, settings.rag_query_max_top_k)`` (the RAG
service contract caps top_k at 20). Chunks below ``agent.rag_min_score`` are dropped
client-side (a belt-and-braces min-score the RAG service also honours). A 403 FORBIDDEN_KB
is surfaced by the client as ``RagResult(forbidden=True)`` — that KB is SKIPPED (not fatal),
the rest continue. The collected chunks are stashed on ``ctx.rag_chunks`` for PROMPT_BUILD
to splice + budget. A transport / non-403 error on a single KB is FAIL-SOFT: it is logged
and that KB is skipped (RAG is an optional enhancement — a retrieval blip must not fail the
task). Cooperative cancel is honoured by the runner BETWEEN stages.

STEP: writes ONE ``rag_query`` audit step (step_type ``rag_query``) summarising
``rag_chunks_returned`` + the KBs queried/forbidden/errored. Per-stage write-through.
"""

from __future__ import annotations

import time

import structlog

from ...db import steps_repo
from ...db.steps_repo import StepRow
from ...models.task import STEP_TYPE_RAG_QUERY
from ..config import get_settings
from ..errors import ApiError
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)


class RagQueryStage(Stage):
    """Query each allowed KB; stash the retrieved chunks for PROMPT_BUILD."""

    name = "RAG_QUERY"

    async def run(self, ctx: PipelineContext) -> None:
        agent = ctx.agent
        if agent is None or not agent.allowed_kb_ids:
            return  # no KBs configured -> RAG is a no-op for this agent (default-disabled shape)

        started = time.monotonic()
        settings = get_settings()
        top_k = min(int(agent.rag_top_k_per_kb), int(settings.rag_query_max_top_k))
        client = deps.get_rag_client()

        queried: list[str] = []
        forbidden: list[str] = []
        errored: list[str] = []
        for kb_id in agent.allowed_kb_ids:
            try:
                result = await client.query(
                    kb_id,
                    ctx.prompt_text,
                    top_k,
                    agent_jwt=ctx.inbound_agent_jwt,
                    on_behalf_of=ctx.principal.agent_id,
                )
            except ApiError as exc:
                # Non-403 transport/service error on ONE KB — fail-soft, skip this KB.
                errored.append(kb_id)
                logger.warning(
                    "rag_query_kb_failed", task_id=ctx.task.task_id, kb_id=kb_id, error=exc.message
                )
                continue
            if result.forbidden:
                forbidden.append(kb_id)  # 403 ACL deny — skip this KB, not fatal
                continue
            queried.append(kb_id)
            for chunk in result.results:
                if chunk.score < agent.rag_min_score:
                    continue
                ctx.rag_chunks.append(
                    {
                        "kb_id": result.kb_id,
                        "chunk_id": chunk.chunk_id,
                        "text": chunk.text,
                        "score": chunk.score,
                        "document_id": chunk.document_id,
                    }
                )

        duration_ms = int((time.monotonic() - started) * 1000)
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type=STEP_TYPE_RAG_QUERY,
                step_name="rag_query",
                status="passed",
                duration_ms=duration_ms,
                output={
                    "rag_chunks_returned": len(ctx.rag_chunks),
                    "kbs_queried": queried,
                    "kbs_forbidden": forbidden,
                    "kbs_errored": errored,
                    "top_k": top_k,
                },
            ),
        )
