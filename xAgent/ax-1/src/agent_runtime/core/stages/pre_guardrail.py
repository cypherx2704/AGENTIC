"""PRE-GUARDRAIL stage — input safety check before the LLM call (Component 4).

Calls the guardrails service ``/v1/check/input`` on ``ctx.prompt_text`` (identity flows
in HEADERS only — the forwarded agent JWT + the xAgent service token; never the body).
The decision maps to the internal audit-step status and drives control flow:

    allow | warn -> passed   (proceed; warn is advisory, not blocking)
    redact       -> redacted (proceed with the redacted text; audit keeps 'redacted')
    block        -> failed   (short-circuit: GUARDRAIL_VIOLATION -> HTTP 422)

Exactly one ``guardrail_check_input`` audit step (step_type ``guardrail_check``) is
appended to ``ctx.steps`` — it is the first of the three first-cycle rows. The
``redacted`` status is preserved here (FIX 2 maps it to ``passed`` only when projecting
the A2A response, not in the audit row).
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

# guardrails decision -> internal task_steps status.
_DECISION_TO_STATUS = {
    "allow": "passed",
    "warn": "passed",
    "redact": "redacted",
    "block": "failed",
}


class PreGuardrailStage(Stage):
    """Run the input guardrail check; redact in place or short-circuit on block."""

    name = "PRE_GUARDRAIL"

    async def run(self, ctx: PipelineContext) -> None:
        started = time.monotonic()
        client = deps.get_guardrails_client()

        result = await client.check_input(
            ctx.prompt_text,
            ctx.task.task_id,
            agent_jwt=ctx.inbound_agent_jwt,
            on_behalf_of=ctx.principal.agent_id,
        )

        # FAIL CLOSED: an unrecognized decision maps to 'failed', never 'passed' — a safety stage
        # must not silently open the gate on an unexpected value (defence in depth behind the
        # client, which already rejects invalid decisions).
        status = _DECISION_TO_STATUS.get(result.decision, "failed")

        if result.decision == "redact" and result.processed_text is not None:
            ctx.prompt_text = result.processed_text

        duration_ms = int((time.monotonic() - started) * 1000)
        # Per-stage WRITE-THROUGH: buffer + persist this step immediately (fail-soft) so
        # GET /v1/tasks/{id} shows it mid-run; EVENT backstops a failed/skipped write.
        await steps_repo.record_step(
            ctx.pool,
            ctx.steps,
            StepRow(
                task_id=ctx.task.task_id,
                tenant_id=ctx.task.tenant_id,
                step_type="guardrail_check",
                step_name="guardrail_check_input",
                status=status,
                duration_ms=duration_ms,
                output={"decision": result.decision, "violations": result.violations},
            ),
        )

        if result.decision == "block":
            logger.info("input_guardrail_blocked", task_id=ctx.task.task_id)
            ctx.fail(
                ErrorCode.GUARDRAIL_VIOLATION,
                "Input blocked by guardrail policy.",
            )
        elif result.decision not in _DECISION_TO_STATUS:
            logger.warning(
                "input_guardrail_unknown_decision",
                task_id=ctx.task.task_id,
                decision=result.decision,
            )
            ctx.fail(
                ErrorCode.INTERNAL_ERROR,
                f"Unknown guardrail decision: {result.decision!r}.",
            )
