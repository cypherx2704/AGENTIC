"""WP02 — LOAD stage agent-status enforcement (Component 1 status transitions).

Runtime config rows carry ``status IN ('active','inactive','pending_config')``; tasks
may execute ONLY against an ``active`` runtime. The REAL :class:`LoadStage` is driven
with ``agents_repo.get_agent`` monkeypatched (no DB needed — the repo seam is the
single read LOAD performs):

  * status=inactive / pending_config -> terminal CONFLICT ('Agent is not active.')
  * status=active                    -> no terminal error; ctx.agent populated and the
                                        prompt text seeded from the task input
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages.load import LoadStage
from agent_runtime.db import agents_repo
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


def _agent(status: str) -> AgentRuntime:
    return AgentRuntime(
        agent_id=AGENT,
        tenant_id=TENANT,
        name="Test Agent",
        status=status,  # type: ignore[arg-type]
        system_prompt="You are helpful.",
    )


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
        steps=StepBuffer(),
        pool=object(),  # type: ignore[arg-type]  # LOAD only forwards it to the patched repo
        started_monotonic=time.monotonic(),
        started_at="2026-06-10T12:00:00.000Z",
    )


def _patch_get_agent(monkeypatch: Any, agent: AgentRuntime | None) -> None:
    async def _fake_get_agent(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime | None:
        assert tenant_id == TENANT
        assert agent_id == AGENT
        return agent

    monkeypatch.setattr(agents_repo, "get_agent", _fake_get_agent)


@pytest.mark.parametrize("status", ["inactive", "pending_config"])
async def test_non_active_agent_rejected_with_conflict(monkeypatch: Any, status: str) -> None:
    _patch_get_agent(monkeypatch, _agent(status))
    ctx = _make_ctx()

    await LoadStage().run(ctx)

    assert ctx.terminal_error is not None
    assert ctx.terminal_error.code == "CONFLICT"
    assert ctx.terminal_error.message == "Agent is not active."
    # The non-active runtime must never be handed to downstream stages.
    assert ctx.agent is None


async def test_active_agent_loads_and_seeds_prompt(monkeypatch: Any) -> None:
    _patch_get_agent(monkeypatch, _agent("active"))
    ctx = _make_ctx()

    await LoadStage().run(ctx)

    assert ctx.terminal_error is None
    assert ctx.agent is not None
    assert ctx.agent.status == "active"
    assert ctx.prompt_text == "hello"


async def test_missing_runtime_row_still_conflicts(monkeypatch: Any) -> None:
    # Regression guard: the pre-existing not-configured path keeps its CONFLICT shape.
    _patch_get_agent(monkeypatch, None)
    ctx = _make_ctx()

    await LoadStage().run(ctx)

    assert ctx.terminal_error is not None
    assert ctx.terminal_error.code == "CONFLICT"
    assert "no runtime configuration" in ctx.terminal_error.message
