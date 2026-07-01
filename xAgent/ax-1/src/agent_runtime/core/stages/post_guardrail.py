"""POST-GUARDRAIL stage — output safety check after the LLM call (Component 4).

Calls the guardrails service ``/v1/check/output`` on ``ctx.final_answer``, passing the
ORIGINAL (pre-redaction) user message as ``input_text`` so the guardrails service can
distinguish an echo of the user's own input from a genuine leak (Phase 4 post-edit on
``output-pii-email-v1``). The original message is read from the immutable task row
(``ctx.task.input['message']``) — NOT from ``ctx.prompt_text``, which PRE-GUARDRAIL may
already have redacted. Identity flows in HEADERS only; the body carries no identity.

Decision -> control flow (same mapping as the input check):

    allow | warn -> passed   (return the answer as-is)
    redact       -> redacted (return the redacted answer; audit keeps 'redacted')
    block        -> failed   (short-circuit: GUARDRAIL_VIOLATION -> HTTP 422)

Appends the third and final first-cycle audit step, ``guardrail_check_output``
(step_type ``guardrail_check``), to ``ctx.steps``.
"""

from __future__ import annotations

import time

import structlog

from ...db import steps_repo
from ...db.steps_repo import StepRow
from ..errors import ErrorCode
from ..pipeline import PipelineContext, Stage
from . import deps

logger = structlog.get_logger(__name__)

_DECISION_TO_STATUS = {
    "allow": "passed",
    "warn": "passed",
    "redact": "redacted",
    "block": "failed",
}


class PostGuardrailStage(Stage):
    """Run the output guardrail check; redact in place or short-circuit on block."""

    name = "POST_GUARDRAIL"

    async def run(self, ctx: PipelineContext) -> None:
        started = time.monotonic()
        client = deps.get_guardrails_client()

        answer = ctx.final_answer or ""
        original_message = ""
        if isinstance(ctx.task.input, dict):
            msg = ctx.task.input.get("message")
            original_message = msg if isinstance(msg, str) else ""

        result = await client.check_output(
            answer,
            original_message,
            ctx.task.task_id,
            agent_jwt=ctx.inbound_agent_jwt,
            on_behalf_of=ctx.principal.agent_id,
        )

        # FAIL CLOSED: an unrecognized decision maps to 'failed', never 'passed' — a safety stage
        # must not silently open the gate on an unexpected value (defence in depth behind the
        # client, which already rejects invalid decisions).
        status = _DECISION_TO_STATUS.get(result.decision, "failed")

        if result.decision == "redact" and result.processed_text is not None:
            ctx.final_answer = result.processed_text

        duration_ms = int((time.monotonic() - started) * 1000)
        # Per-stage WRITE-THROUGH: buffer + persist immediately (fail-soft) so an in-flight
        # GET /v1/tasks/{id} shows this step; EVENT backstops a failed/skipped write.
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type="guardrail_check",
                step_name="guardrail_check_output",
                status=status,
                duration_ms=duration_ms,
                output={"decision": result.decision, "violations": result.violations},
            ),
        )

        if result.decision == "block":
            logger.info("output_guardrail_blocked", task_id=ctx.task.task_id)
            ctx.fail(
                ErrorCode.GUARDRAIL_VIOLATION,
                "Output blocked by guardrail policy.",
            )
        elif result.decision not in _DECISION_TO_STATUS:
            logger.warning(
                "output_guardrail_unknown_decision",
                task_id=ctx.task.task_id,
                decision=result.decision,
            )
            ctx.fail(
                ErrorCode.INTERNAL_ERROR,
                f"Unknown guardrail decision: {result.decision!r}.",
            )
