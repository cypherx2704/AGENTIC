"""TOOL_LOOP server-name vs tool-name mapping (Contract-4 MCP naming) — regression suite.

Bug: xAgent resolved a tool by its SERVER name (dash-case, e.g. ``tool-web-search``) and then
offered the LLM that same server name AND sent it as the invoke ``tool`` field — but an MCP
server's invoke ``tool`` field must be one of its declared ``manifest.tools[].name`` (snake_case,
e.g. ``web_search``). Server-name != tool-name -> the tool server 404s the invoke.

Fix (``_resolve_tools`` / ``_tools_of``): resolve by server name, but offer + invoke by the
tool name(s) declared in ``manifest.tools[]``. A single MCP server may host MANY tools; each is
offered under its own name. A legacy manifest with no ``tools[]`` falls back to one tool named
after the server (preserving the prior single-tool behaviour).

Drives the REAL :class:`ToolLoopStage` against fake clients; asserts the invoke ``tool`` field is
the TOOL name, and that a multi-tool server offers every tool. Network-/DB-free.
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
TRACE_ID = "22222222-2222-2222-2222-222222222222"


# A Contract-4 manifest: server name (dash-case) + a tool declared under a DIFFERENT snake_case name.
def _manifest(server_name: str, tool_names: list[str]) -> dict[str, Any]:
    return {
        "name": server_name,
        "description": f"{server_name} server",
        "tools": [
            {
                "name": tn,
                "description": f"the {tn} tool",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
            for tn in tool_names
        ],
        "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
    }


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
        # `tool` here is EXACTLY what xAgent sends as the tools/call `name` — the assertion target.
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
        manifest=_manifest(server_name, tool_names), invoke_url="http://tool",
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


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


# ── the core regression: allowed_tools=[server-name] offers + invokes the TOOL name ─────
async def test_server_name_entry_offers_and_invokes_tool_name() -> None:
    # allowed_tools lists the SERVER name; the manifest's tool is named differently.
    registry = _FakeRegistry(resolutions={"tool-web-search": _resolution("tool-web-search", ["web_search"])})
    mcp = _FakeMcp(results={"web_search": McpResult(tool="web_search", result={"hits": 1})})
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="web_search", arguments={"query": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(_agent(["tool-web-search"])))

    # Registry was resolved by the SERVER name...
    assert registry.calls == [("tool-web-search", None)]
    # ...but the LLM was offered the TOOL name...
    offered_names = [t["function"]["name"] for t in llms.offered[0]]
    assert offered_names == ["web_search"], offered_names
    # ...and the invoke `tool` field is the TOOL name (NOT the server name) — the 404 fix.
    assert len(mcp.calls) == 1
    assert mcp.calls[0]["tool"] == "web_search"


# ── a multi-tool server offers EVERY tool it declares ───────────────────────────────────
async def test_multi_tool_server_offers_all_tools() -> None:
    registry = _FakeRegistry(
        resolutions={"tool-suite": _resolution("tool-suite", ["web_search", "fetch_url"])}
    )
    mcp = _FakeMcp(results={})
    llms = _FakeLlms(completions=[_completion(content="answered directly")])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(_agent(["tool-suite"])))

    offered_names = sorted(t["function"]["name"] for t in llms.offered[0])
    assert offered_names == ["fetch_url", "web_search"]


# ── a manifest with no `mcp` transport is DROPPED (MCP is the only wire; no fallback) ────
async def test_manifest_without_mcp_descriptor_is_dropped() -> None:
    no_mcp = ToolResolution(
        name="legacy-tool", version="1.0.0",
        manifest={"description": "no mcp descriptor",
                  "tools": [{"name": "legacy-tool", "input_schema": {"type": "object"}}]},
        invoke_url="http://tool",
    )
    registry = _FakeRegistry(resolutions={"legacy-tool": no_mcp})
    mcp = _FakeMcp(results={})
    llms = _FakeLlms(completions=[])  # never called: nothing resolves -> loop no-ops
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(_agent(["legacy-tool"])))

    assert mcp.calls == []      # not invoked (no MCP endpoint advertised)
    assert llms.offered == []   # not offered — the loop returned early with nothing resolved
