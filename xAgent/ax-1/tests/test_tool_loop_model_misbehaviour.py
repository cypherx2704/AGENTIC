"""TOOL_LOOP — the loop survives a model that fumbles the tool protocol.

Both failures below were observed live against Groq + ``llama-3.1-8b-instant`` and each one
KILLED the whole orchestration with a terminal ``VALIDATION_ERROR``:

  1. the model called ``brave_search`` — a tool it was never offered (it is not in any agent's
     ``allowed_tools``; the model simply knows the name). The loop appended that call to the
     assistant turn, so the NEXT request carried a tool_call absent from ``tools[]`` and Groq
     rejected the request outright: "attempted to call tool 'brave_search' which was not in
     request.tools".
  2. the model emitted a malformed native call and Groq answered 400 ``tool_use_failed``
     ("Failed to call a function"), which the loop treated as fatal.

A weak model is an ASSUMPTION of this system (agents run 8B models), so neither may be terminal.
These tests pin the two guarantees:

  * a tool_call for an un-offered tool NEVER enters the message history — it is dropped from the
    assistant turn and answered with a plain-dialogue correction;
  * a provider ``tool_use_failed`` is retried ONCE in emulated tool mode (which takes tools[] off
    the wire entirely), and only a second failure is terminal.

Fakes + harness mirror ``test_wp12_tool_loop.py`` (real ToolLoopStage, fake clients via deps).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runtime.core import config
from agent_runtime.core.auth import Principal
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpResult
from agent_runtime.services.registry_client import ToolResolution

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


@dataclass
class _FakeRegistry:
    resolutions: dict[str, Any]

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        outcome = self.resolutions[name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class _FakeMcp:
    results: dict[str, Any]
    calls: list[str] = field(default_factory=list)

    async def invoke_mcp(self, mcp_url: str, tool: str, args: dict[str, Any], **kw: Any) -> McpResult:
        self.calls.append(tool)
        return self.results.get(tool) or McpResult(tool=tool, result={"ok": True})


@dataclass
class _FakeLlms:
    """chat() pops the next scripted outcome; a scripted Exception is raised.

    Records the messages AND the ``tool_mode`` of every turn, so a test can assert both what the
    history looked like and which tool-calling mode the gateway was asked for.
    """

    outcomes: list[Any]  # ChatCompletion | Exception
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    modes: list[str | None] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.calls.append([dict(m) for m in messages])
        self.modes.append(kw.get("tool_mode"))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion(content: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content,
        finish_reason="tool_calls" if tool_calls else "stop",
        model="groq-llama",
        usage=Usage(total_tokens=0, cost_usd=0.0),
        tool_calls=tool_calls or [],
        raw={},
    )


def _tool(name: str) -> ToolResolution:
    return ToolResolution(
        name=name,
        version="1.0.0",
        invoke_url="http://tool",
        manifest={
            "description": name,
            "tools": [{"name": name, "description": name, "input_schema": {"type": "object"}}],
            "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
        },
    )


def _ctx() -> PipelineContext:
    agent = AgentRuntime(
        agent_id=AGENT, tenant_id=TENANT, name="wiki-researcher", system_prompt="s",
        allowed_tools=["wikipedia_summary"], llm_model="groq-llama",
    )
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt="jwt",
        trace_id=TRACE_ID,
        request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id=TRACE_ID,
                     status="running", input={"message": "go"}),
        agent=agent,
        prompt_text="go",
        messages=[{"role": "user", "content": "go"}],
        steps=StepBuffer(),
        pool=None,
        started_monotonic=time.monotonic(),
    )


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    """Unbind the fake clients after each test so they never leak into another module."""
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


def _tool_use_failed() -> ApiError:
    """The Groq 400 as the gateway hands it to us: its own code flattens to VALIDATION_ERROR,
    the PROVIDER's code survives in details.provider_code."""
    return ApiError(
        ErrorCode.VALIDATION_ERROR,
        "Upstream provider 'groq' rejected the request as invalid: Failed to call a function.",
        status_code=400,
        details={"upstream_status": 400, "upstream_code": "VALIDATION_ERROR",
                 "provider_code": "tool_use_failed"},
    )


def _native(monkeypatch: Any) -> None:
    """Pin native tool mode so the emulated FALLBACK is what is under test (not the default)."""
    monkeypatch.setattr(config.get_settings(), "tool_loop_tool_mode", "native", raising=False)


def _assistant_turns(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in turn if m.get("role") == "assistant" and m.get("tool_calls")]


def _called_names(turn: list[dict[str, Any]]) -> set[str]:
    return {
        tc.get("function", {}).get("name")
        for m in _assistant_turns(turn)
        for tc in m.get("tool_calls", [])
    }


# ── 1. a hallucinated tool never enters the history ─────────────────────────────────
async def test_unoffered_tool_call_never_enters_history(monkeypatch: Any) -> None:
    _native(monkeypatch)
    registry = _FakeRegistry(resolutions={"wikipedia_summary": _tool("wikipedia_summary")})
    mcp = _FakeMcp(results={})
    llms = _FakeLlms(outcomes=[
        # The model invents a tool it was never given (the live failure).
        _completion(tool_calls=[ToolCall(id="c1", name="brave_search", arguments={"q": "react"})]),
        _completion(content="Answered without the invented tool."),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx()

    await ToolLoopStage().run(ctx)

    # The task SURVIVES and answers.
    assert ctx.terminal_error is None
    assert ctx.final_answer == "Answered without the invented tool."
    # The invented call was never dispatched...
    assert mcp.calls == []
    # ...and — the crux — it is absent from the history sent on the NEXT turn, so no provider can
    # reject the request for carrying a tool_call that is not in tools[].
    second_turn = llms.calls[1]
    assert "brave_search" not in _called_names(second_turn)
    assert _assistant_turns(second_turn) == []
    # The model was told, in plain dialogue, what it may actually call.
    correction = [
        m for m in second_turn
        if m.get("role") == "user" and "brave_search" in str(m.get("content"))
    ]
    assert len(correction) == 1
    assert "wikipedia_summary" in str(correction[0]["content"])
    # It is still audited as a failed tool_call.
    steps = [s for s in ctx.steps.steps if s.step_type == STEP_TYPE_TOOL_CALL]
    assert len(steps) == 1
    assert steps[0].status == "failed"
    assert steps[0].output["error"] == "tool_not_allowed"


# ── 2. a mixed turn keeps the real call and drops only the invented one ─────────────
async def test_mixed_turn_keeps_offered_call_drops_unoffered(monkeypatch: Any) -> None:
    _native(monkeypatch)
    registry = _FakeRegistry(resolutions={"wikipedia_summary": _tool("wikipedia_summary")})
    mcp = _FakeMcp(results={"wikipedia_summary": McpResult(tool="wikipedia_summary", result={"x": 1})})
    llms = _FakeLlms(outcomes=[
        _completion(tool_calls=[
            ToolCall(id="c1", name="wikipedia_summary", arguments={"topic": "react"}),
            ToolCall(id="c2", name="brave_search", arguments={"q": "react"}),
        ]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx()

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is None
    assert mcp.calls == ["wikipedia_summary"]  # only the real one ran

    second_turn = llms.calls[1]
    assert _called_names(second_turn) == {"wikipedia_summary"}  # the invented one is gone

    # History invariant: EVERY tool_call in the assistant turn is answered by a tool message with
    # the same id (and no tool message answers a call that is not there). A provider rejects the
    # request otherwise — this is the property the old code violated.
    call_ids = {tc["id"] for m in _assistant_turns(second_turn) for tc in m["tool_calls"]}
    result_ids = {m.get("tool_call_id") for m in second_turn if m.get("role") == "tool"}
    assert call_ids == result_ids == {"c1"}


# ── 3. a provider tool_use_failed falls back to emulated instead of dying ───────────
async def test_tool_use_failed_falls_back_to_emulated(monkeypatch: Any) -> None:
    _native(monkeypatch)
    registry = _FakeRegistry(resolutions={"wikipedia_summary": _tool("wikipedia_summary")})
    mcp = _FakeMcp(results={})
    llms = _FakeLlms(outcomes=[
        _tool_use_failed(),                       # native turn: Groq cannot parse the model's call
        _completion(content="Recovered answer."),  # emulated retry of the SAME turn
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx()

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is None
    assert ctx.final_answer == "Recovered answer."
    # The retry is the same turn re-issued in emulated mode — the mode the gateway needs to take
    # tools[] off the wire so the provider's tool parser is never involved.
    assert llms.modes == ["native", "emulated"]


async def test_tool_use_failed_twice_is_terminal(monkeypatch: Any) -> None:
    _native(monkeypatch)
    registry = _FakeRegistry(resolutions={"wikipedia_summary": _tool("wikipedia_summary")})
    llms = _FakeLlms(outcomes=[_tool_use_failed(), _tool_use_failed()])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=_FakeMcp(results={}))
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx()

    await ToolLoopStage().run(ctx)

    # Emulation is the last resort; a failure there is a real failure.
    assert ctx.terminal_error is not None
    assert llms.modes == ["native", "emulated"]


# ── 4. an unrelated LLM error stays terminal and is NOT retried ─────────────────────
async def test_other_llm_error_is_terminal_without_retry(monkeypatch: Any) -> None:
    _native(monkeypatch)
    registry = _FakeRegistry(resolutions={"wikipedia_summary": _tool("wikipedia_summary")})
    rate_limited = ApiError(
        ErrorCode.RATE_LIMIT_EXCEEDED, "provider rate-limited", status_code=429,
        details={"upstream_status": 429, "upstream_code": "RATE_LIMIT_EXCEEDED", "provider_code": None},
    )
    llms = _FakeLlms(outcomes=[rate_limited])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=_FakeMcp(results={}))
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx()

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is not None
    assert ctx.terminal_error.code == ErrorCode.RATE_LIMIT_EXCEEDED
    assert llms.modes == ["native"]  # emulation cannot fix a rate-limit — do not burn a second call
