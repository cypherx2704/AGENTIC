"""WP02 — finish_reason validation in the LLM stage.

The gateway's ``finish_reason`` is validated against the known unified enum
(``Settings.llm_known_finish_reasons``, env-overridable; the in-code default mirrors
the llms-gateway ``FinishReason`` Literal). The REAL :class:`LlmStage` runs against an
in-test fake LLMs client bound via ``stages.deps.set_clients`` (the same seam the api
lifespan wires):

  * ``stop``     -> passed; step output records finish_reason, no warning
  * ``length``   -> passed; step output carries ``warning: "truncated"`` (truncation
                    is surfaced to audit, the task still completes)
  * unknown      -> passed (treated as 'stop'); RAW value preserved in the step output
                    (``finish_reason_raw``) + ``warning: "unknown_finish_reason"``;
                    the pipeline does NOT fail
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.llm import LlmStage
from agent_runtime.db.steps_repo import StepBuffer, StepRow
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


@dataclass
class _FakeUsage:
    total_tokens: int = 20
    cost_usd: float = 0.0021


@dataclass
class _FakeCompletion:
    content: str | None
    finish_reason: str | None
    usage: _FakeUsage = field(default_factory=_FakeUsage)


@dataclass
class _FakeLlms:
    completion: _FakeCompletion

    async def chat(self, **_: Any) -> _FakeCompletion:
        return self.completion


@pytest.fixture
def _restore_clients() -> Iterator[None]:
    """Snapshot + restore the module-level client holder around each test."""
    original_guardrails = deps._guardrails_client
    original_llms = deps._llms_client
    try:
        yield
    finally:
        deps.set_clients(guardrails_client=original_guardrails, llms_client=original_llms)


def _make_ctx() -> PipelineContext:
    return PipelineContext(
        principal=Principal(
            tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="agent.jwt"
        ),
        inbound_agent_jwt="agent.jwt",
        trace_id=TRACE_ID,
        request_id="req-1",
        task=TaskRow(
            task_id=TASK_ID,
            agent_id=AGENT,
            tenant_id=TENANT,
            trace_id=TRACE_ID,
            status="running",
            input={"message": "hello"},
        ),
        agent=AgentRuntime(
            agent_id=AGENT, tenant_id=TENANT, name="Test Agent", system_prompt="You are helpful."
        ),
        messages=[{"role": "user", "content": "hello"}],
        steps=StepBuffer(),
        started_monotonic=time.monotonic(),
        started_at="2026-06-10T12:00:00.000Z",
    )


async def _run_llm_stage(finish_reason: str | None) -> tuple[PipelineContext, StepRow]:
    fake = _FakeLlms(_FakeCompletion(content="The answer is 4.", finish_reason=finish_reason))
    deps.set_clients(guardrails_client=None, llms_client=fake)  # type: ignore[arg-type]
    ctx = _make_ctx()
    await LlmStage().run(ctx)
    assert ctx.steps is not None and len(ctx.steps.steps) == 1
    return ctx, ctx.steps.steps[0]


async def test_finish_reason_stop_passes_without_warning(_restore_clients: None) -> None:
    ctx, step = await _run_llm_stage("stop")
    assert ctx.terminal_error is None
    assert ctx.final_answer == "The answer is 4."
    assert step.status == "passed"
    assert step.output == {"finish_reason": "stop"}  # recorded; no warning field


async def test_finish_reason_length_records_truncation_warning(_restore_clients: None) -> None:
    ctx, step = await _run_llm_stage("length")
    # Truncation does NOT fail the task — it surfaces as a warning in the audit output.
    assert ctx.terminal_error is None
    assert ctx.final_answer == "The answer is 4."
    assert step.status == "passed"
    assert step.output == {"finish_reason": "length", "warning": "truncated"}


async def test_unknown_finish_reason_treated_as_stop_with_raw_audit(_restore_clients: None) -> None:
    ctx, step = await _run_llm_stage("banana")
    # Unknown -> warn + treat as 'stop'; the RAW gateway value is preserved for audit.
    assert ctx.terminal_error is None
    assert ctx.final_answer == "The answer is 4."
    assert step.status == "passed"
    assert step.output == {
        "finish_reason": "stop",
        "finish_reason_raw": "banana",
        "warning": "unknown_finish_reason",
    }


async def test_none_finish_reason_is_unknown(_restore_clients: None) -> None:
    ctx, step = await _run_llm_stage(None)
    assert ctx.terminal_error is None
    assert step.output is not None
    assert step.output["warning"] == "unknown_finish_reason"
    assert step.output["finish_reason_raw"] is None
