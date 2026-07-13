"""WP12 — TOOL_LOOP stage (``core/stages/tool_loop.py``).

Drives the REAL :class:`ToolLoopStage` against FAKE RegistryClient + McpClient + LlmsClient
injected via the deps seam. No network / DB (``ctx.pool`` None disables the metering INSERT
unless we attach a recording fake pool to observe outbox writes).

Coverage:
  * a tool-call loop runs invoke with Idempotency derived from (task_id, tool_call_id),
    feeds the result back, and ends on a final (tool-less) answer;
  * version-pin enforcement: ``name@version`` only resolves/invokes that exact version; a
    pin MISMATCH drops the tool from the offered set;
  * ``max_iterations`` reached while the model still wants tools -> ``tool_loop_limit`` step
    (NOT an error; the partial answer stands);
  * the multi-call invocation budget short-circuits BUDGET_EXCEEDED (terminal);
  * a cost budget overrun short-circuits BUDGET_EXCEEDED;
  * one ``tool_call`` audit step per invocation + one metered outbox row per invocation
    (observed via a recording fake pool);
  * a failed tool invocation is FAIL-SOFT to the loop (error fed back, not fatal).

The retry-on-5xx-not-4xx behaviour is owned by McpClient and unit-tested in
``test_wp12_clients.py`` (here the fake McpClient stands in for the client).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runtime.core.auth import Principal
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL, STEP_TYPE_TOOL_LOOP_LIMIT
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpResult
from agent_runtime.services.registry_client import ToolResolution

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


# ── Fakes ────────────────────────────────────────────────────────────────────────────
@dataclass
class _FakeRegistry:
    """resolve_tool returns the scripted ToolResolution per (name) or raises."""

    resolutions: dict[str, Any]  # name -> ToolResolution | Exception
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        self.calls.append((name, version))
        outcome = self.resolutions[name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class _FakeMcp:
    """invoke_mcp returns a scripted McpResult per tool-name or raises an ApiError (fail-soft)."""

    results: dict[str, Any]  # tool name -> McpResult | Exception
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def invoke_mcp(self, mcp_url: str, tool: str, args: dict[str, Any], *, task_id: str,
                         tool_call_id: str, agent_jwt: str, on_behalf_of: str | None = None) -> McpResult:
        self.calls.append({
            "mcp_url": mcp_url, "tool": tool, "args": args,
            "task_id": task_id, "tool_call_id": tool_call_id,
            "idempotency_key": f"{task_id}:{tool_call_id}",
        })
        outcome = self.results.get(tool)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or McpResult(tool=tool, result={"ok": True})


@dataclass
class _FakeLlms:
    """chat() returns the next scripted ChatCompletion from a queue."""

    completions: list[ChatCompletion]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.calls.append(list(messages))
        return self.completions.pop(0)


@dataclass
class _RecordingPool:
    """A stand-in for the psycopg pool that records metered-event INSERTs via in_tenant.

    ``db.outbox.record_metered_event`` calls ``in_tenant(pool, tenant_id, _txn)`` which does
    ``async with pool.connection() as conn, conn.transaction(): conn.execute(...)``. This
    double honours that exact shape and captures each INSERT so the test can assert one
    metered row per invocation without a real DB.
    """

    inserts: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def connection(self) -> Any:
        return _RecordingConn(self)


class _AsyncNullCtx:
    """An async context manager that yields nothing (for ``conn.transaction()``)."""

    async def __aenter__(self) -> _AsyncNullCtx:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _RecordingConn:
    def __init__(self, pool: _RecordingPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _RecordingConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def transaction(self) -> _AsyncNullCtx:
        return _AsyncNullCtx()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        # in_tenant issues SET app.tenant_id first, then the INSERT — record only the INSERT.
        if "INSERT INTO xagent.outbox" in sql:
            self._pool.inserts.append((sql, params))
        return None


def _completion(content: str | None = None, tool_calls: list[ToolCall] | None = None,
                cost: float = 0.0, tokens: int = 0) -> ChatCompletion:
    return ChatCompletion(
        content=content, finish_reason="tool_calls" if tool_calls else "stop", model="smart",
        usage=Usage(total_tokens=tokens, cost_usd=cost), tool_calls=tool_calls or [], raw={},
    )


def _tool(name: str, version: str = "1.0.0", url: str = "http://tool") -> ToolResolution:
    return ToolResolution(
        name=name, version=version, invoke_url=url,
        manifest={
            "description": name,
            "tools": [{"name": name, "description": name, "input_schema": {"type": "object"}}],
            "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
        },
    )


def _agent(allowed_tools: list[str]) -> AgentRuntime:
    return AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                        allowed_tools=allowed_tools, llm_model="smart")


def _ctx(agent: AgentRuntime, *, pool: Any = None, cost_budget: float | None = None) -> PipelineContext:
    ctx = PipelineContext(
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
        pool=pool,
        started_monotonic=time.monotonic(),
        cost_budget_usd=cost_budget,
    )
    return ctx


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


def _steps(ctx: PipelineContext, step_type: str) -> list[Any]:
    return [s for s in ctx.steps.steps if s.step_type == step_type]


# ── tool-call loop: invoke once, feed back, finish on a tool-less answer ────────────
async def test_tool_loop_invokes_then_final_answer() -> None:
    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result={"hits": 3})})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="call-1", name="search", arguments={"q": "x"})]),
        _completion(content="Final answer using the tool result."),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    pool = _RecordingPool()
    ctx = _ctx(_agent(["search"]), pool=pool)

    await ToolLoopStage().run(ctx)

    # One invocation; idempotency key = task_id:tool_call_id.
    assert len(mcp.calls) == 1
    assert mcp.calls[0]["idempotency_key"] == f"{TASK_ID}:call-1"
    assert mcp.calls[0]["mcp_url"] == "http://tool/mcp"
    # Final answer set; no terminal error.
    assert ctx.final_answer == "Final answer using the tool result."
    assert ctx.terminal_error is None
    # One tool_call audit step + one metered outbox row.
    tool_steps = _steps(ctx, STEP_TYPE_TOOL_CALL)
    assert len(tool_steps) == 1
    assert tool_steps[0].status == "passed"
    assert tool_steps[0].output["tool_call_id"] == "call-1"
    assert len(pool.inserts) == 1  # exactly one tools.invocation.metered row
    assert ctx.tool_invocations == 1
    # The tool result was fed back to the model on the 2nd turn (a 'tool' role message).
    second_turn = llms.calls[1]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call-1" for m in second_turn)


# ── version-pin enforcement: only the pinned version is offered/invoked ─────────────
async def test_version_pin_match_resolves_and_invokes() -> None:
    registry = _FakeRegistry(resolutions={"search": _tool("search", version="2.1.0")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result="ok")})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c", name="search", arguments={})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["search@2.1.0"]))

    await ToolLoopStage().run(ctx)

    # The pin was passed to resolve_tool, version matched -> tool offered + invoked.
    assert registry.calls == [("search", "2.1.0")]
    assert len(mcp.calls) == 1
    assert _steps(ctx, STEP_TYPE_TOOL_CALL)[0].output["tool_version"] == "2.1.0"


async def test_version_pin_mismatch_drops_tool() -> None:
    # Registry resolves a DIFFERENT version than pinned -> the tool is dropped (not offered).
    registry = _FakeRegistry(resolutions={"search": _tool("search", version="9.9.9")})
    mcp = _FakeMcp(results={})
    # No tools resolved -> the loop returns early WITHOUT calling the LLM.
    llms = _FakeLlms(completions=[])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["search@1.0.0"]))

    await ToolLoopStage().run(ctx)

    assert registry.calls == [("search", "1.0.0")]
    assert mcp.calls == []  # the mismatched tool was never invoked
    assert llms.calls == []  # no tools resolved -> base LLM answer stands, loop no-ops
    assert ctx.terminal_error is None


# ── max_iterations -> tool_loop_limit step (partial answer, NOT an error) ────────────
async def test_max_iterations_records_tool_loop_limit(monkeypatch: Any) -> None:
    from agent_runtime.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "tool_loop_max_iterations", 3, raising=False)

    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result="r")})
    # The model ALWAYS asks for a tool -> never produces a final answer -> hit the limit.
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id=f"c{i}", name="search", arguments={})]) for i in range(3)
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["search"]))

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is None  # the limit is NOT an error
    limit_steps = _steps(ctx, STEP_TYPE_TOOL_LOOP_LIMIT)
    assert len(limit_steps) == 1
    assert limit_steps[0].output["max_iterations"] == 3
    assert llms.calls and len(llms.calls) == 3  # exactly the iteration cap


# ── multi-call invocation budget -> BUDGET_EXCEEDED (terminal) ──────────────────────
async def test_invocation_budget_short_circuits_budget_exceeded(monkeypatch: Any) -> None:
    from agent_runtime.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "tool_loop_max_invocations", 2, raising=False)
    monkeypatch.setattr(settings, "tool_loop_max_iterations", 10, raising=False)

    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result="r")})
    # First turn requests THREE calls; the 3rd crosses the cap of 2 -> BUDGET_EXCEEDED.
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[
            ToolCall(id="a", name="search", arguments={}),
            ToolCall(id="b", name="search", arguments={}),
            ToolCall(id="c", name="search", arguments={}),
        ]),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["search"]))

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is not None
    assert ctx.terminal_error.code == ErrorCode.BUDGET_EXCEEDED
    assert ctx.tool_invocations == 2  # only the first two ran before the cap tripped
    assert len(mcp.calls) == 2


# ── cost budget overrun -> BUDGET_EXCEEDED ──────────────────────────────────────────
async def test_cost_budget_short_circuits() -> None:
    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result="r")})
    # First LLM turn already costs more than the budget -> short-circuit before invoking.
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c", name="search", arguments={})], cost=0.50),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["search"]), cost_budget=0.10)

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is not None
    assert ctx.terminal_error.code == ErrorCode.BUDGET_EXCEEDED
    assert mcp.calls == []  # the cost cap tripped before any invocation


# ── failed tool invocation is FAIL-SOFT to the loop (error fed back, not fatal) ─────
async def test_failed_invocation_is_fed_back_not_fatal() -> None:
    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": ApiError(ErrorCode.VALIDATION_ERROR, "bad args")})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="search", arguments={})]),
        _completion(content="Recovered without the tool."),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["search"]))

    await ToolLoopStage().run(ctx)

    assert ctx.terminal_error is None  # a failed tool call is fail-soft to the loop
    assert ctx.final_answer == "Recovered without the tool."
    # A failed tool_call step was recorded; the error was fed back to the model.
    tool_step = _steps(ctx, STEP_TYPE_TOOL_CALL)[0]
    assert tool_step.status == "failed"
    second_turn = llms.calls[1]
    tool_msg = next(m for m in second_turn if m.get("role") == "tool")
    assert "error" in tool_msg["content"]


# ── metered outbox row emitted per invocation (failed invocations included) ─────────
async def test_metered_event_per_invocation() -> None:
    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result="r")})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[
            ToolCall(id="a", name="search", arguments={}),
            ToolCall(id="b", name="search", arguments={}),
        ]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    pool = _RecordingPool()
    ctx = _ctx(_agent(["search"]), pool=pool)

    await ToolLoopStage().run(ctx)

    assert len(mcp.calls) == 2
    assert len(pool.inserts) == 2  # one metered outbox row per invocation
    # The metered payload carries the tool + tool_call_id (Contract 5 envelope, Jsonb-wrapped).
    envelopes = [params[2].obj for _sql, params in pool.inserts]  # Jsonb.obj is the dict
    metered_tools = [e["payload"]["tool"] for e in envelopes]
    assert metered_tools == ["search", "search"]
    # tool_call_id flows from the model's call.id ("a", "b") into the metered payload.
    assert [e["payload"]["tool_call_id"] for e in envelopes] == ["a", "b"]
    assert envelopes[0]["event_type"]  # full Contract-5 envelope wraps the payload


# ── default-disabled shape: no allowed_tools -> the base LLM answer stands (no-op) ──
async def test_no_tools_is_noop() -> None:
    registry = _FakeRegistry(resolutions={})
    mcp = _FakeMcp(results={})
    llms = _FakeLlms(completions=[])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent([]))

    await ToolLoopStage().run(ctx)

    assert registry.calls == []
    assert llms.calls == []
    assert _steps(ctx, STEP_TYPE_TOOL_CALL) == []
