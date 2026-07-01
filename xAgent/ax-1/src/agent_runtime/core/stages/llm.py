"""LLM stage — the single first-cycle model round-trip (Component 5).

Calls the llms-gateway ``/v1/chat/completions`` once (no tool loop in first cycle) with
the agent's configured model and ``effective_max_tokens()`` (= min(max_tokens,
token_budget_per_task) — the single-call budget cap). On success it sets
``ctx.final_answer`` and accumulates ``ctx.tokens_used`` + ``ctx.cost_usd`` (cost is
computed by the gateway from per-provider pricing and surfaced in the usage block).

Identity flows in HEADERS only (forwarded agent JWT + xAgent service token); the body
carries no identity. Exactly one ``llm_call`` audit step (step_type ``llm_call``,
carrying tokens) is appended to ``ctx.steps`` — the second of the three first-cycle
rows — recorded on BOTH the success and the failure path:

    provider success -> passed ; provider/transport error -> failed ; timeout -> timeout

``finish_reason`` validation (WP02 amended fix): the gateway's value is validated
against the known unified enum (``Settings.llm_known_finish_reasons``, env-overridable).
An UNKNOWN value is logged + counted and treated as ``stop``, with the raw value
preserved in the step output (audit). Truncation (``length``) proceeds but records a
``warning: "truncated"`` field in the step output.

A provider/transport failure short-circuits the pipeline with an INTERNAL_ERROR
terminal error (the downstream client surfaces transport failures as SERVICE_UNAVAILABLE
``ApiError``; we record the step, then mark the task failed). EVENT still runs.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from ...db import steps_repo
from ...db.steps_repo import StepRow
from .. import metrics
from ..config import get_settings
from ..errors import ApiError, ErrorCode
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)

# The gateway's truncation finish_reason (max_tokens hit). A protocol constant of the
# llms-gateway unified enum, not tunable config — referenced for the warning special-case.
_FINISH_REASON_TRUNCATED = "length"


class LlmStage(Stage):
    """Single chat-completion round-trip; record the llm_call audit step."""

    name = "LLM"

    async def run(self, ctx: PipelineContext) -> None:
        started = time.monotonic()
        client = deps.get_llms_client()
        agent = ctx.agent

        # LOAD guarantees ctx.agent is set before LLM runs (it short-circuits otherwise),
        # but guard defensively so an unexpected ordering fails cleanly, not with AttributeError.
        if agent is None:
            ctx.fail(ErrorCode.INTERNAL_ERROR, "Agent runtime config missing at LLM stage.")
            return

        status = "failed"
        tokens = 0
        try:
            completion = await client.chat(
                model=agent.llm_model,
                messages=ctx.messages,
                max_tokens=agent.effective_max_tokens(),
                temperature=agent.temperature,
                agent_jwt=ctx.inbound_agent_jwt,
                on_behalf_of=ctx.principal.agent_id,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            status = "timeout"
            logger.warning("llm_call_timeout", task_id=ctx.task.task_id, error=str(exc))
            await self._record_step(ctx, status, tokens, time.monotonic() - started)
            ctx.fail(ErrorCode.SERVICE_UNAVAILABLE, "LLM call timed out.", status="timeout")
            return
        except ApiError as exc:
            # The downstream client wraps transport/timeout HTTP errors as SERVICE_UNAVAILABLE.
            status = "timeout" if exc.status_code == 504 else "failed"
            logger.warning("llm_call_failed", task_id=ctx.task.task_id, error=exc.message)
            await self._record_step(ctx, status, tokens, time.monotonic() - started)
            ctx.fail(
                exc.code,
                exc.message,
                status="timeout" if status == "timeout" else "failed",
            )
            return

        # finish_reason validation (WP02): validate against the known gateway enum.
        # Unknown -> log + count + treat as 'stop', raw value preserved in the audit
        # step output; 'length' (truncation) -> warning field in the step output.
        raw_finish = completion.finish_reason
        known_reasons = get_settings().known_finish_reasons()
        step_output: dict[str, Any] = {"finish_reason": raw_finish}
        if raw_finish not in known_reasons:
            metrics.llm_finish_reason_unknown_total.inc()
            logger.warning(
                "llm_unknown_finish_reason",
                task_id=ctx.task.task_id,
                finish_reason=raw_finish,
            )
            step_output = {
                "finish_reason": "stop",  # treated as a normal stop (amended fix)
                "finish_reason_raw": raw_finish,  # audit: the gateway's actual value
                "warning": "unknown_finish_reason",
            }
        elif raw_finish == _FINISH_REASON_TRUNCATED:
            logger.warning("llm_response_truncated", task_id=ctx.task.task_id)
            step_output["warning"] = "truncated"

        ctx.final_answer = completion.content
        tokens = completion.usage.total_tokens
        ctx.tokens_used += tokens
        ctx.cost_usd += completion.usage.cost_usd
        status = "passed"
        await self._record_step(ctx, status, tokens, time.monotonic() - started, output=step_output)

        # Cost-budget enforcement (WP12): if the task set a per-task USD cost cap and this
        # call pushed accrued cost over it, short-circuit BUDGET_EXCEEDED (EVENT marks the
        # task failed). No cap (cost_budget_usd is None) -> unchanged first-cycle behaviour.
        if ctx.cost_budget_usd is not None and ctx.cost_usd > ctx.cost_budget_usd:
            logger.warning(
                "llm_cost_budget_exceeded",
                task_id=ctx.task.task_id,
                cost_usd=ctx.cost_usd,
                budget=ctx.cost_budget_usd,
            )
            ctx.fail(ErrorCode.BUDGET_EXCEEDED, "Task exceeded its cost budget.", status="failed")

    @staticmethod
    async def _record_step(
        ctx: PipelineContext,
        status: str,
        tokens: int,
        elapsed_s: float,
        *,
        output: dict[str, Any] | None = None,
    ) -> None:
        # Per-stage WRITE-THROUGH (buffer + immediate fail-soft INSERT). Recorded on BOTH
        # the success and failure paths so an in-flight GET shows the llm_call row.
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type="llm_call",
                step_name="llm_call",
                status=status,
                duration_ms=int(elapsed_s * 1000),
                tokens_used=tokens,
                output=output,
            ),
        )
