"""TOOL_LOOP against a MULTI-tool MCP server (Phase-3 reconcile proof).

In the aggregating-MCP model an ``allowed_tools`` entry names ONE MCP *server* whose Contract-4
manifest lists MANY member tools (``manifest.tools[]``), served by tool-flow-bridge at
``POST /m/<slug>/mcp``. This suite drives the REAL :class:`ToolLoopStage` against fake clients to
prove, concretely, that xAgent handles the multi-tool case (not just the single-tool one):

  * a single allowed_tools entry naming ``mcp-suite`` resolves ONCE and offers ALL its member
    tools (``alpha`` + ``beta``) — server_name vs per-tool tool_name kept distinct;
  * a ``tools/call`` for a chosen member routes to that server's ``/m/<slug>/mcp`` endpoint with
    the invoke ``name`` = the MEMBER tool name (not the server name);
  * per-capability access is keyed by (server_name, capability=member tool name): within ONE MCP,
    ``alpha`` (automated) is invoked while sibling ``beta`` (none) is denied;
  * a registry error while resolving a member's access FAILS CLOSED (deny), even for a sibling
    whose access would otherwise be automated.

Network-/DB-free (``ctx.pool`` is None so no metering INSERT is attempted).
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
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpResult
from agent_runtime.services.registry_client import ToolResolution

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"

# The MCP server slug drives the aggregating wire path: POST /m/<slug>/mcp.
SERVER = "mcp-suite"
INVOKE_URL = "http://bridge"
MCP_ENDPOINT = f"/m/{SERVER}/mcp"  # tool-flow-bridge's aggregating endpoint (Contract-4 `mcp` descriptor)


def _manifest(server_name: str, tool_names: list[str]) -> dict[str, Any]:
    """A Contract-4 aggregating manifest: one server hosting MANY member tools."""
    return {
        "name": server_name,
        "description": f"{server_name} aggregating server",
        "tools": [
            {
                "name": tn,
                "description": f"the {tn} member tool",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
            for tn in tool_names
        ],
        "mcp": {"transport": "streamable-http", "endpoint": MCP_ENDPOINT},
    }


@dataclass
class _FakeRegistry:
    """resolve_tool returns one ToolResolution per server; get_tool_access is per-(server, capability)."""

    resolutions: dict[str, Any]
    # (server_name, capability) -> none|ask|automated | Exception(raise to prove fail-closed)
    access: dict[tuple[str, str | None], Any] = field(default_factory=dict)
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    access_calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        self.calls.append((name, version))
        return self.resolutions[name]

    async def get_tool_access(
        self, name: str, *, capability: str | None = None, agent_jwt: str = "",
        on_behalf_of: str | None = None,
    ) -> str:
        self.access_calls.append((name, capability))
        outcome = self.access.get((name, capability), "automated")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class _FakeMcp:
    """invoke_mcp records the endpoint + invoke `name` (the assertion targets) per call."""

    results: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def invoke_mcp(self, mcp_url: str, tool: str, args: dict[str, Any], *, task_id: str,
                         tool_call_id: str, agent_jwt: str, on_behalf_of: str | None = None) -> McpResult:
        self.calls.append({"mcp_url": mcp_url, "tool": tool, "args": args})
        return self.results.get(tool) or McpResult(tool=tool, result={"ok": True})


@dataclass
class _FakeLlms:
    completions: list[ChatCompletion]
    offered: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.offered.append(kw.get("tools") or [])
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


def _resolution(server_name: str, tool_names: list[str]) -> ToolResolution:
    return ToolResolution(
        name=server_name, version="1.0.0",
        manifest=_manifest(server_name, tool_names), invoke_url=INVOKE_URL,
    )


def _agent(allowed_tools: list[str]) -> AgentRuntime:
    return AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                        llm_model="smart", allowed_tools=allowed_tools)


def _ctx(agent: AgentRuntime) -> PipelineContext:
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt="jwt", trace_id=TRACE_ID, request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id=TRACE_ID,
                     status="running", input={"message": "go"}),
        agent=agent, prompt_text="go", messages=[{"role": "user", "content": "go"}],
        steps=StepBuffer(), pool=None, started_monotonic=time.monotonic(), cost_budget_usd=None,
    )


def _steps(ctx: PipelineContext, step_type: str) -> list[Any]:
    return [s for s in ctx.steps.steps if s.step_type == step_type]


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


# ── 1a: ONE allowed_tools entry -> resolve once -> offer ALL member tools ────────────────
async def test_one_entry_resolves_once_and_offers_all_members() -> None:
    registry = _FakeRegistry(resolutions={SERVER: _resolution(SERVER, ["alpha", "beta"])})
    mcp = _FakeMcp()
    llms = _FakeLlms(completions=[_completion(content="answered directly")])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(_agent([SERVER])))

    # The single server entry resolved EXACTLY once (not once-per-member).
    assert registry.calls == [(SERVER, None)]
    # ...and BOTH member tools were offered to the LLM under their own (member) names.
    offered_names = sorted(t["function"]["name"] for t in llms.offered[0])
    assert offered_names == ["alpha", "beta"]


# ── 1b: tools/call for a chosen member routes to /m/<slug>/mcp with name=member ──────────
async def test_tools_call_routes_member_by_name_to_mcp_endpoint() -> None:
    registry = _FakeRegistry(resolutions={SERVER: _resolution(SERVER, ["alpha", "beta"])})
    mcp = _FakeMcp(results={"beta": McpResult(tool="beta", result={"picked": "beta"})})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="beta", arguments={"q": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(_agent([SERVER])))

    # Exactly one invoke, routed to the SERVER's aggregating endpoint with name=the MEMBER tool.
    assert len(mcp.calls) == 1
    assert mcp.calls[0]["tool"] == "beta"                       # invoke `name` = member, not server
    assert mcp.calls[0]["mcp_url"] == f"{INVOKE_URL}{MCP_ENDPOINT}"  # http://bridge/m/mcp-suite/mcp
    # Access was resolved keyed by (server_name, capability=member name).
    assert registry.access_calls == [(SERVER, "beta")]


# ── 1c: per-capability access within ONE MCP — alpha automated invoked, beta none denied ─
async def test_per_capability_access_denies_beta_invokes_alpha() -> None:
    registry = _FakeRegistry(
        resolutions={SERVER: _resolution(SERVER, ["alpha", "beta"])},
        access={(SERVER, "alpha"): "automated", (SERVER, "beta"): "none"},
    )
    mcp = _FakeMcp(results={"alpha": McpResult(tool="alpha", result={"ok": True})})
    # The model requests BOTH members in one turn; access is resolved per member.
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[
            ToolCall(id="a", name="alpha", arguments={"q": "x"}),
            ToolCall(id="b", name="beta", arguments={"q": "y"}),
        ]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent([SERVER]))

    await ToolLoopStage().run(ctx)

    # Only alpha (automated) actually invoked; beta (none) was denied BEFORE any invoke.
    assert [c["tool"] for c in mcp.calls] == ["alpha"]
    # Access lookups: BOTH keyed on the SERVER name, capability = the MEMBER tool name.
    assert registry.access_calls == [(SERVER, "alpha"), (SERVER, "beta")]
    # The denied sibling produced a failed tool_call step with tool_access_denied; alpha passed.
    steps = {s.output["tool"]: s for s in _steps(ctx, STEP_TYPE_TOOL_CALL)}
    assert steps["alpha"].status == "passed"
    assert steps["beta"].status == "failed"
    assert steps["beta"].output["error"] == "tool_access_denied"
    assert ctx.terminal_error is None


# ── 1c (fail-closed): a registry error resolving a member's access DENIES that member ────
async def test_access_registry_error_fails_closed_for_member() -> None:
    registry = _FakeRegistry(
        resolutions={SERVER: _resolution(SERVER, ["alpha", "beta"])},
        access={
            (SERVER, "alpha"): "automated",
            (SERVER, "beta"): ApiError(ErrorCode.SERVICE_UNAVAILABLE, "registry down"),
        },
    )
    mcp = _FakeMcp(results={"alpha": McpResult(tool="alpha", result={"ok": True})})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[
            ToolCall(id="a", name="alpha", arguments={}),
            ToolCall(id="b", name="beta", arguments={}),
        ]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent([SERVER]))

    await ToolLoopStage().run(ctx)

    # beta's access lookup raised -> fail CLOSED (deny) -> not invoked; alpha still runs.
    assert [c["tool"] for c in mcp.calls] == ["alpha"]
    beta_step = next(s for s in _steps(ctx, STEP_TYPE_TOOL_CALL) if s.output["tool"] == "beta")
    assert beta_step.status == "failed"
    assert beta_step.output["error"] == "tool_access_denied"


# ── dedupe: two servers exposing the SAME tool name -> keep the first, deterministic dispatch ──
async def test_duplicate_tool_name_across_servers_keeps_first() -> None:
    """Two DISTINCT servers in allowed_tools each expose a tool named ``web_search`` (the retired
    ``tool-web-search`` and its flow-tool replacement). The loop must offer ONE ``web_search`` and
    dispatch to the FIRST server in allowed_tools — never silently last-wins on the by_name map."""
    registry = _FakeRegistry(
        resolutions={
            "srv-old": _resolution("srv-old", ["web_search"]),
            "srv-new": _resolution("srv-new", ["web_search"]),
        }
    )
    mcp = _FakeMcp()
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="web_search", arguments={"q": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(_agent(["srv-old", "srv-new"])))

    # Exactly ONE web_search offered to the LLM (the duplicate is deduped, not a two-entry array).
    assert [t["function"]["name"] for t in llms.offered[0]] == ["web_search"]
    # Exactly one invoke, and access + dispatch resolved against the FIRST server (srv-old), not last.
    assert len(mcp.calls) == 1
    assert registry.access_calls == [("srv-old", "web_search")]
