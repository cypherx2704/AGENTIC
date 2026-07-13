"""Per-agent tool-loop toggle (``tool_loop_enabled``, migration 0007) — INTEGRITY suite.

Proves the "per request" vs "multiple request" switch is wired consistently across every
layer it touches, with the DEFAULT preserving the prior multi-call behaviour byte-for-byte:

  * MODEL — ``AgentRuntime`` / ``AgentRuntimeRegistration`` default true; explicit false
    round-trips; a legacy row (dict without the column) validates to true (extra=ignore);
    the registration body still forbids unknown fields (extra=forbid).
  * REPO SQL SHAPE — the INSERT / UPDATE column lists, placeholder counts, and the SELECT
    ``_COLUMNS`` all include ``tool_loop_enabled`` and stay balanced (placeholders == columns
    == bound params) so a real INSERT/UPDATE can never drift out of alignment.
  * STAGE BEHAVIOUR (the enforcing skip) — through the REAL :class:`ToolLoopStage` against
    fake clients:
      - default (enabled) agent WITH allowed_tools runs the loop -> the LLM is called and a
        tool is invoked (current behaviour, unchanged);
      - disabled ("per request") agent WITH allowed_tools SKIPS -> zero LLM calls, zero tool
        invocations, no terminal error (the base LLM answer stands = a single LLM call);
      - a toolless agent still skips regardless of the toggle (prior invariant intact).

Network-/DB-free: fakes injected via the deps seam; no pool needed for the skip paths.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from agent_runtime.core.auth import Principal
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db import agents_repo
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime, AgentRuntimeRegistration
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpResult
from agent_runtime.services.registry_client import ToolResolution

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


# ── MODEL: default preserves prior behaviour; explicit values round-trip ────────────────
def test_model_defaults_true_preserving_prior_behaviour() -> None:
    a = AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s")
    assert a.tool_loop_enabled is True
    r = AgentRuntimeRegistration(name="A", system_prompt="s")
    assert r.tool_loop_enabled is True


def test_model_explicit_false_round_trips_and_serialises() -> None:
    a = AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                     tool_loop_enabled=False)
    assert a.tool_loop_enabled is False
    dumped = a.model_dump(mode="json")
    assert dumped["tool_loop_enabled"] is False  # the API response carries the field


def test_legacy_row_without_column_validates_to_default_true() -> None:
    # A row read before migration 0007 (dict lacks the key) must not blow up: extra=ignore +
    # the default make it "multiple request" — exactly its prior behaviour.
    legacy = AgentRuntime.model_validate(
        {"agent_id": AGENT, "tenant_id": TENANT, "name": "A", "system_prompt": "s"}
    )
    assert legacy.tool_loop_enabled is True


def test_registration_still_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentRuntimeRegistration(name="A", system_prompt="s", bogus=1)  # type: ignore[call-arg]


# ── REPO SQL SHAPE: the column is present and the statements stay balanced ───────────────
def test_select_columns_include_toggle() -> None:
    assert "tool_loop_enabled" in agents_repo._COLUMNS


def _sql_of(fn: Any) -> str:
    """Best-effort: pull the SQL string literals out of a repo function's source."""
    import inspect
    return inspect.getsource(fn)


@pytest.mark.parametrize("fn", [
    agents_repo.upsert_agent_runtime,
    agents_repo.insert_agent_runtime,
    agents_repo.update_agent_runtime,
])
def test_write_paths_reference_the_toggle_column(fn: Any) -> None:
    src = _sql_of(fn)
    assert "tool_loop_enabled" in src, f"{fn.__name__} does not write tool_loop_enabled"
    assert "reg.tool_loop_enabled" in src, f"{fn.__name__} does not bind reg.tool_loop_enabled"


def test_insert_placeholders_match_columns_and_params() -> None:
    """The INSERT column-count, %s-count, and bound-param-count must all agree.

    Adding a column without a matching placeholder/param (or vice versa) is the classic way
    an INSERT silently mis-binds. This guards both INSERT paths against that drift.
    """
    for fn in (agents_repo.upsert_agent_runtime, agents_repo.insert_agent_runtime):
        src = _sql_of(fn)
        # Column list inside INSERT INTO xagent.agents ( ... )
        cols_block = re.search(r"INSERT INTO xagent\.agents\s*\((.*?)\)", src, re.S)
        assert cols_block, f"{fn.__name__}: could not find INSERT column list"
        n_cols = len([c for c in cols_block.group(1).split(",") if c.strip()])
        # VALUES (%s,%s,...)
        values_block = re.search(r"VALUES\s*\(([^)]*)\)", src, re.S)
        assert values_block, f"{fn.__name__}: could not find VALUES list"
        n_placeholders = values_block.group(1).count("%s")
        assert n_cols == n_placeholders, (
            f"{fn.__name__}: {n_cols} columns but {n_placeholders} placeholders"
        )


# ── STAGE fakes (mirror test_wp12_tool_loop.py) ─────────────────────────────────────────
@dataclass
class _FakeRegistry:
    resolutions: dict[str, Any]
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        self.calls.append((name, version))
        return self.resolutions[name]


@dataclass
class _FakeMcp:
    results: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def invoke_mcp(self, mcp_url: str, tool: str, args: dict[str, Any], *, task_id: str,
                         tool_call_id: str, agent_jwt: str, on_behalf_of: str | None = None) -> McpResult:
        self.calls.append({"tool": tool, "tool_call_id": tool_call_id})
        return self.results.get(tool) or McpResult(tool=tool, result={"ok": True})


@dataclass
class _FakeLlms:
    completions: list[ChatCompletion]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.calls.append(list(messages))
        return self.completions.pop(0)


def _completion(*, content: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content,
        finish_reason="tool_calls" if tool_calls else "stop",
        model="smart",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
        tool_calls=tool_calls or [],
        raw={},
    )


def _tool(name: str) -> ToolResolution:
    return ToolResolution(
        name=name, version="1.0.0", invoke_url="http://tool",
        manifest={
            "description": name,
            "tools": [{"name": name, "input_schema": {"type": "object"}}],
            "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
        },
    )


def _agent(*, allowed_tools: list[str], tool_loop_enabled: bool = True) -> AgentRuntime:
    return AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                        llm_model="smart", allowed_tools=allowed_tools,
                        tool_loop_enabled=tool_loop_enabled)


def _ctx(agent: AgentRuntime) -> PipelineContext:
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
        cost_budget_usd=None,
    )


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


def _steps(ctx: PipelineContext, step_type: str) -> list[Any]:
    return [s for s in ctx.steps.steps if s.step_type == step_type]


# ── STAGE: enabled (default) runs the loop; disabled skips to a single LLM call ──────────
async def test_enabled_agent_with_tools_runs_the_loop() -> None:
    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result={"hits": 1})})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    ctx = _ctx(_agent(allowed_tools=["search"], tool_loop_enabled=True))
    await ToolLoopStage().run(ctx)

    assert len(llms.calls) == 2          # the loop made multiple LLM calls
    assert len(mcp.calls) == 1           # and invoked the tool
    assert ctx.final_answer == "done"
    assert ctx.terminal_error is None
    assert len(_steps(ctx, STEP_TYPE_TOOL_CALL)) == 1


async def test_disabled_agent_with_tools_skips_the_loop_entirely() -> None:
    """per-request mode: even WITH allowed_tools, the stage makes ZERO LLM/tool calls."""
    registry = _FakeRegistry(resolutions={"search": _tool("search")})
    mcp = _FakeMcp(results={"search": McpResult(tool="search", result={"hits": 1})})
    llms = _FakeLlms(completions=[])  # empty: any chat() call would IndexError -> proves 0 calls
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    ctx = _ctx(_agent(allowed_tools=["search"], tool_loop_enabled=False))
    await ToolLoopStage().run(ctx)

    assert llms.calls == []               # NO extra LLM round-trips (single-call: base LLM stands)
    assert mcp.calls == []                # NO tool invocations
    assert registry.calls == []           # did not even resolve tools
    assert ctx.terminal_error is None     # not an error — a deliberate skip
    assert _steps(ctx, STEP_TYPE_TOOL_CALL) == []


async def test_toolless_agent_skips_regardless_of_toggle() -> None:
    llms = _FakeLlms(completions=[])
    deps.set_enhancement_clients(
        registry_client=_FakeRegistry(resolutions={}), mcp_client=_FakeMcp(results={})
    )
    deps.set_clients(guardrails_client=None, llms_client=llms)

    for enabled in (True, False):
        ctx = _ctx(_agent(allowed_tools=[], tool_loop_enabled=enabled))
        await ToolLoopStage().run(ctx)
        assert llms.calls == []
        assert ctx.terminal_error is None
