"""Real-MCP (JSON-RPC 2.0 / Streamable HTTP) transport — client wire + TOOL_LOOP selection.

Two layers, both network-/DB-free:

* ``McpClient.invoke_mcp`` — the handshake (``initialize`` -> ``tools/call``) over respx-mocked
  HTTP: result parsing, identity + Idempotency-Key headers, and ``isError`` -> ApiError mapping
  (retryable => SERVICE_UNAVAILABLE, else VALIDATION_ERROR).
* ``ToolLoopStage`` — a manifest advertising an ``mcp`` descriptor routes the invocation through
  ``invoke_mcp`` (real MCP) instead of the legacy ``invoke`` wire, at the right endpoint URL.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

from agent_runtime.core.auth import Principal
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpClient, McpResult
from agent_runtime.services.registry_client import ToolResolution

AGENT_JWT = "inbound.agent.jwt"
ON_BEHALF = "00000000-0000-0000-0000-0000000000bb"
MCP_ENDPOINT = "http://tool-x/mcp"


class _FakeTokens:
    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        return "svc.jwt.token"

    async def aclose(self) -> None:
        return None


def _settings(**overrides: Any) -> Settings:
    base = get_settings().model_dump()
    base.update(overrides)
    return Settings(**base)


def _mcp(settings: Settings) -> tuple[McpClient, httpx.AsyncClient]:
    http = httpx.AsyncClient()
    return McpClient(settings, _FakeTokens(), client=http), http


def _init_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": "mcp-init",
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tool-web-search", "version": "0.1.0"},
            },
        },
    )


def _call_response(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "tc-1", "result": result})


# ════════════════════════════ McpClient.invoke_mcp wire ════════════════════════════
@respx.mock
async def test_invoke_mcp_handshake_and_result() -> None:
    client, http = _mcp(_settings(mcp_retry_attempts=0))
    route = respx.post(MCP_ENDPOINT).mock(
        side_effect=[
            _init_response(),
            _call_response(
                {
                    "content": [{"type": "text", "text": json.dumps({"results": [{"rank": 1}]})}],
                    "structuredContent": {"results": [{"rank": 1}]},
                    "isError": False,
                }
            ),
        ]
    )
    try:
        result = await client.invoke_mcp(
            MCP_ENDPOINT, "web_search", {"query": "x"},
            task_id="task-1", tool_call_id="tc-1", agent_jwt=AGENT_JWT, on_behalf_of=ON_BEHALF,
        )
        assert isinstance(result, McpResult)
        assert result.result == {"results": [{"rank": 1}]}
        # initialize + tools/call = two POSTs to the one MCP endpoint.
        assert route.call_count == 2
        # Identity + idempotency ride as headers (Contract 12/13/9) on the tools/call POST.
        call_headers = route.calls[1].request.headers
        assert call_headers["idempotency-key"] == "task-1:tc-1"
        assert call_headers["x-forwarded-agent-jwt"] == AGENT_JWT
        assert call_headers["authorization"] == "Bearer svc.jwt.token"
        # The tools/call body is JSON-RPC 2.0 selecting the tool by name.
        body = json.loads(route.calls[1].request.content)
        assert body["method"] == "tools/call"
        assert body["params"] == {"name": "web_search", "arguments": {"query": "x"}}
    finally:
        await http.aclose()


@respx.mock
async def test_invoke_mcp_iserror_retryable_maps_to_service_unavailable() -> None:
    client, http = _mcp(_settings(mcp_retry_attempts=0))
    respx.post(MCP_ENDPOINT).mock(
        side_effect=[
            _init_response(),
            _call_response(
                {
                    "content": [{"type": "text", "text": "provider down"}],
                    "isError": True,
                    "_meta": {"code": "SERVICE_UNAVAILABLE", "retryable": True},
                }
            ),
        ]
    )
    try:
        with pytest.raises(ApiError) as ei:
            await client.invoke_mcp(
                MCP_ENDPOINT, "web_search", {"query": "x"},
                task_id="t", tool_call_id="tc-1", agent_jwt=AGENT_JWT, on_behalf_of=ON_BEHALF,
            )
        assert ei.value.code == ErrorCode.SERVICE_UNAVAILABLE
    finally:
        await http.aclose()


@respx.mock
async def test_invoke_mcp_iserror_terminal_maps_to_validation_error() -> None:
    client, http = _mcp(_settings(mcp_retry_attempts=0))
    respx.post(MCP_ENDPOINT).mock(
        side_effect=[
            _init_response(),
            _call_response(
                {
                    "content": [{"type": "text", "text": "bad args"}],
                    "isError": True,
                    "_meta": {"code": "VALIDATION_ERROR", "retryable": False},
                }
            ),
        ]
    )
    try:
        with pytest.raises(ApiError) as ei:
            await client.invoke_mcp(
                MCP_ENDPOINT, "web_search", {}, task_id="t", tool_call_id="tc-1",
                agent_jwt=AGENT_JWT, on_behalf_of=ON_BEHALF,
            )
        assert ei.value.code == ErrorCode.VALIDATION_ERROR
    finally:
        await http.aclose()


# ════════════════════════════ TOOL_LOOP transport selection ════════════════════════════
TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"


@dataclass
class _FakeRegistry:
    resolution: ToolResolution

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> ToolResolution:
        return self.resolution


@dataclass
class _FakeMcp:
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)

    async def invoke_mcp(self, mcp_url: str, tool: str, args: dict[str, Any], **kw: Any) -> McpResult:
        self.mcp_calls.append({"mcp_url": mcp_url, "tool": tool})
        return McpResult(tool=tool, result={"via": "mcp"})


@dataclass
class _FakeLlms:
    completions: list[ChatCompletion]

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        return self.completions.pop(0)


def _completion(*, content: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content, finish_reason="tool_calls" if tool_calls else "stop", model="smart",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
        tool_calls=tool_calls or [], raw={},
    )


def _ctx(allowed_tools: list[str]) -> PipelineContext:
    agent = AgentRuntime(agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
                         llm_model="smart", allowed_tools=allowed_tools)
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


async def test_manifest_with_mcp_descriptor_routes_through_invoke_mcp() -> None:
    manifest = {
        "name": "tool-web-search",
        "description": "web search",
        "tools": [{"name": "web_search", "description": "search",
                   "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}],
        "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
    }
    registry = _FakeRegistry(
        ToolResolution(name="tool-web-search", version="1.0.0", manifest=manifest, invoke_url="http://tool-x")
    )
    mcp = _FakeMcp()
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="web_search", arguments={"query": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(["tool-web-search"]))

    # Routed through real MCP at {invoke_url}{mcp.endpoint}.
    assert mcp.mcp_calls == [{"mcp_url": "http://tool-x/mcp", "tool": "web_search"}]


async def test_manifest_without_mcp_descriptor_is_dropped() -> None:
    manifest = {
        "name": "tool-legacy",
        "description": "no mcp",
        "tools": [{"name": "do_thing", "description": "d", "input_schema": {"type": "object"}}],
    }
    registry = _FakeRegistry(
        ToolResolution(name="tool-legacy", version="1.0.0", manifest=manifest, invoke_url="http://tool-y")
    )
    mcp = _FakeMcp()
    llms = _FakeLlms(completions=[])  # never called: nothing resolves (MCP is the only wire)
    deps.set_enhancement_clients(registry_client=registry, mcp_client=mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    await ToolLoopStage().run(_ctx(["tool-legacy"]))

    # No mcp descriptor -> the tool is dropped; nothing invoked.
    assert mcp.mcp_calls == []
