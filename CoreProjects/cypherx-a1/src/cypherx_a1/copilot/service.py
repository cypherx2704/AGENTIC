"""The AI copilot — cited answers over the engineering memory.

Flow (your decided design: call llms-gateway + guardrails DIRECTLY, with a clean seam to
later route through xAgent):

    memory recall → PRE-guardrail(question) → hybrid retrieve → prompt build →
    llms chat → POST-guardrail(answer, input_text=question) → store episodic memory →
    cited answer

Guardrails are **fail-closed** (a 5xx/invalid decision raises) and a ``block`` maps to
``422 GUARDRAIL_VIOLATION``; a ``redact`` swaps in the processed text. Memory is best-effort
(an outage never fails an answer). Every answer carries the retrieval citations so an
autonomous agent can verify the source.
"""

from __future__ import annotations

import time
import uuid

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import trace
from ..core import metrics as m
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..models.api import AskResponse
from ..services.guardrails_client import GuardrailsClient
from ..services.llms_client import LlmsClient
from ..services.memory_client import MemoryClient
from ..retrieval.orchestrator import RetrievalOrchestrator

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are CypherX engineering-memory copilot. Answer the engineer's question about THIS "
    "organisation's codebase, services, decisions, and people using ONLY the provided "
    "context. Be concise and concrete. If the context is insufficient, say so plainly "
    "rather than guessing. Refer to specific repos/PRs/services/people by name when the "
    "context supports it."
)


class CopilotService:
    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        settings: Settings,
        guardrails: GuardrailsClient,
        llms: LlmsClient,
        memory: MemoryClient,
        orchestrator: RetrievalOrchestrator,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._guardrails = guardrails
        self._llms = llms
        self._memory = memory
        self._orch = orchestrator

    async def ask(
        self,
        *,
        tenant_id: str,
        agent_jwt: str,
        agent_id: str | None,
        question: str,
        session_id: str | None,
        top_k: int,
    ) -> AskResponse:
        started = time.monotonic()
        task_id = str(uuid.uuid4())
        s = self._settings

        # 1) Conversational memory recall (best-effort).
        memory_block = ""
        if s.copilot_memory_enabled:
            if session_id:
                await self._memory.ensure_session(
                    session_id=session_id, agent_jwt=agent_jwt, on_behalf_of=agent_id
                )
            prior = await self._memory.search(
                query=question, top_k=3, agent_jwt=agent_jwt, on_behalf_of=agent_id
            )
            if prior:
                memory_block = "\n".join(f"- {p.content}" for p in prior)

        # 2) PRE-guardrail (fail-closed; block -> 422).
        gi = await self._guardrails.check_input(
            question, task_id, agent_jwt=agent_jwt, on_behalf_of=agent_id
        )
        if gi.decision == "block":
            m.copilot_requests_total.labels("blocked_input").inc()
            raise ApiError(ErrorCode.GUARDRAIL_VIOLATION, "Question blocked by input guardrails.")
        question_eff = gi.processed_text if gi.decision == "redact" and gi.processed_text else question

        # 3) Hybrid retrieve (graph + rag-dense + keyword, RRF, cited).
        retrieval = await self._orch.retrieve(
            self._pool, tenant_id=tenant_id, agent_jwt=agent_jwt, agent_id=agent_id,
            question=question_eff, top_k=top_k,
        )

        # 4) Prompt build (system prompt + memory + retrieved context never truncated away).
        context = retrieval.context_text()
        user_parts = []
        if memory_block:
            user_parts.append(f"Earlier in this conversation:\n{memory_block}")
        user_parts.append(f"Context from engineering memory:\n{context or '(no matching context found)'}")
        user_parts.append(f"Question: {question_eff}")
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

        # 5) LLM answer.
        completion = await self._llms.chat(
            model=s.copilot_model, messages=messages, max_tokens=s.copilot_max_tokens,
            temperature=s.copilot_temperature, agent_jwt=agent_jwt, on_behalf_of=agent_id,
        )
        answer = completion.content or ""

        # 6) POST-guardrail (input_text distinguishes echoed PII; block -> 422; redact -> swap).
        go = await self._guardrails.check_output(
            answer, question_eff, task_id, agent_jwt=agent_jwt, on_behalf_of=agent_id
        )
        if go.decision == "block":
            m.copilot_requests_total.labels("blocked_output").inc()
            raise ApiError(ErrorCode.GUARDRAIL_VIOLATION, "Answer blocked by output guardrails.")
        if go.decision == "redact" and go.processed_text:
            answer = go.processed_text

        # 7) Store episodic memory (best-effort).
        if s.copilot_memory_enabled:
            await self._memory.store(
                content=f"Q: {question_eff}\nA: {answer[:800]}",
                memory_type=s.copilot_memory_type, session_id=session_id,
                agent_jwt=agent_jwt, on_behalf_of=agent_id,
                idempotency_key=f"{task_id}:mem",
            )

        m.copilot_requests_total.labels("ok").inc()
        duration_ms = int((time.monotonic() - started) * 1000)
        m.copilot_latency_seconds.observe(duration_ms / 1000)
        return AskResponse(
            answer=answer,
            citations=retrieval.citations(),
            used=retrieval.used,
            trace_id=trace.trace_id_var.get(),
            duration_ms=duration_ms,
        )
