"""TOOL_LOOP — the RUN-level tool switch (``PipelineContext.use_tools``).

The caller's "Use Tools" choice on POST /v1/orchestrations reaches every task in the run. OFF means
the run is a plain chat completion: the stage must not resolve, offer, or invoke a single tool.

"Off" has to mean UNREACHABLE, not merely unused — a tools-off run that still called the Tool
Registry to resolve schemas (and only then declined to use them) would leak the tenant's tool
catalogue into a run the user explicitly asked to keep tool-free, and would bill the round-trips.
So the guard is asserted to fire BEFORE resolution, not just before invocation.

The run-level switch and the agent-level ``tool_loop_enabled`` (migration 0007) are independent:
either one alone turns tools off. Neither can re-enable what the other disabled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpResult
from agent_runtime.services.registry_client import ToolResolution

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"


@dataclass
class _FakeRegistry:
    """Records every resolve — a tools-off run must never reach here."""

    resolved: list[str] = field(default_factory=list)

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        self.resolved.append(name)
        return ToolResolution(
            name=name, version="1.0.0", invoke_url="http://tool",
            manifest={
                "description": name,
                "tools": [{"name": name, "description": name, "input_schema": {"type": "object"}}],
                "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
            },
        )


@dataclass
class _FakeMcp:
    invoked: list[str] = field(default_factory=list)

    async def invoke_mcp(self, mcp_url: str, tool: str, args: dict[str, Any], **kw: Any) -> McpResult:
        self.invoked.append(tool)
        return McpResult(tool=tool, result={"ok": True})


@dataclass
class _FakeLlms:
    outcomes: list[ChatCompletion]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    tools_offered: list[Any] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.calls.append(list(messages))
        self.tools_offered.append(kw.get("tools"))
        return self.outcomes.pop(0)


def _completion(content: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content, finish_reason="tool_calls" if tool_calls else "stop", model="groq-llama",
        usage=Usage(total_tokens=0, cost_usd=0.0), tool_calls=tool_calls or [], raw={},
    )


def _ctx(*, use_tools: bool, tool_loop_enabled: bool = True) -> PipelineContext:
    agent = AgentRuntime(
        agent_id=AGENT, tenant_id=TENANT, name="repo-analyst", system_prompt="s",
        allowed_tools=["github_repo_info"], llm_model="groq-llama",
        tool_loop_enabled=tool_loop_enabled,
    )
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt="jwt",
        trace_id="t", request_id="r",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id="t",
                     status="running", input={"message": "go"}),
        agent=agent,
        prompt_text="go",
        messages=[{"role": "user", "content": "go"}],
        steps=StepBuffer(),
        pool=None,
        started_monotonic=time.monotonic(),
        use_tools=use_tools,
    )


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


async def test_use_tools_off_resolves_offers_and_invokes_nothing() -> None:
    registry, mcp = _FakeRegistry(), _FakeMcp()
    llms = _FakeLlms(outcomes=[])  # the stage must not call the LLM either — it returns immediately
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(use_tools=False)

    await ToolLoopStage().run(ctx)

    # Nothing resolved: "off" means the tools are unreachable, not merely unused. (If the guard
    # were placed after _resolve_tools, this list would hold "github_repo_info".)
    assert registry.resolved == []
    assert mcp.invoked == []
    assert llms.calls == []          # no loop turn at all — the base LLM stage's answer stands
    assert ctx.terminal_error is None
    assert ctx.tool_invocations == 0


async def test_use_tools_on_runs_the_loop_as_before() -> None:
    """The control: the SAME agent with the switch ON still resolves, offers and invokes."""
    registry, mcp = _FakeRegistry(), _FakeMcp()
    llms = _FakeLlms(outcomes=[
        _completion(tool_calls=[ToolCall(id="c1", name="github_repo_info", arguments={"owner": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(use_tools=True)

    await ToolLoopStage().run(ctx)

    assert registry.resolved == ["github_repo_info"]
    assert mcp.invoked == ["github_repo_info"]
    assert ctx.final_answer == "done"
    assert llms.tools_offered[0]  # the schemas really were offered to the model


async def test_agent_level_toggle_still_vetoes_independently() -> None:
    """use_tools=True cannot re-enable an agent whose own tool_loop_enabled is False."""
    registry, mcp = _FakeRegistry(), _FakeMcp()
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=_FakeLlms(outcomes=[]))
    ctx = _ctx(use_tools=True, tool_loop_enabled=False)

    await ToolLoopStage().run(ctx)

    assert registry.resolved == []
    assert mcp.invoked == []
