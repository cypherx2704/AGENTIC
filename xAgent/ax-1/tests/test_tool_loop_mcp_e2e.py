"""END-TO-END: a (scripted) LLM calls tools via REAL MCP over a REAL transport.

Every OTHER tool-loop test fakes the MCP client (``_FakeMcp``) or mocks HTTP with respx. This
suite is the missing join: it drives the **REAL** :class:`ToolLoopStage` and the **REAL**
:class:`McpClient` against a **REAL in-process MCP server** (a spec-compliant Streamable-HTTP /
JSON-RPC 2.0 app served over ``httpx.ASGITransport`` — no sockets, no DB, no keys). So the wire

    scripted LLM tool_call
      -> ToolLoopStage._invoke_one
      -> McpClient.invoke_mcp  (POST initialize -> POST tools/call, real JSON-RPC over ASGI)
      -> MCP server routes by name, runs the tool, returns a cited result
      -> McpClient parses it -> fed back as a role:"tool" message
      -> scripted LLM final answer

is exercised for real, end to end. The in-test server mirrors the production tool servers'
protocol (``tool-flow-bridge`` ``/{m,w}/<slug>/mcp`` and ``mcp-eng-memory`` ``/mcp`` — whose OWN
suites prove they implement the same wire), so this proves the agent half interoperates with a
server that speaks it.

Scenarios: happy path (result cited + fed back + metered), JSON vs SSE (text/event-stream)
response bodies, the ``initialize`` session handshake (``Mcp-Session-Id`` echoed on tools/call),
identity headers (Contract 12/13) riding the wire, ``isError`` fail-soft (retryable + terminal),
an HTTP-4xx tool server (fail-soft), multi-tool dispatch, and access-denied short-circuit
(no invoke ever reaches the server).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_runtime.core.auth import Principal
from agent_runtime.core.config import Settings, get_settings
from agent_runtime.core.pipeline import PipelineContext
from agent_runtime.core.stages import deps
from agent_runtime.core.stages.tool_loop import ToolLoopStage
from agent_runtime.db.steps_repo import StepBuffer
from agent_runtime.db.tasks_repo import TaskRow
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.models.task import STEP_TYPE_TOOL_CALL
from agent_runtime.services.llms_client import ChatCompletion, ToolCall, Usage
from agent_runtime.services.mcp_client import McpClient

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
TASK_ID = "11111111-1111-1111-1111-111111111111"
TRACE_ID = "22222222-2222-2222-2222-222222222222"
SVC_JWT = "svc.jwt.token"
AGENT_JWT = "inbound.agent.jwt"

# The registry-resolved base + the manifest's mcp.endpoint. The stage builds
# mcp_url = INVOKE_URL + "/mcp"; ASGITransport routes it into the app by PATH ("/mcp").
INVOKE_URL = "http://mcp-server"


# ════════════════════════════ A REAL in-process MCP server ════════════════════════════
def _rpc_body(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_response(req_id: Any, result: dict[str, Any], *, sse: bool, headers: dict[str, str]) -> Response:
    """A JSON-RPC result as either an application/json body or a Streamable-HTTP SSE frame."""
    payload = json.dumps(_rpc_body(req_id, result))
    if sse:
        body = f"event: message\ndata: {payload}\n\n"
        return Response(body, media_type="text/event-stream", headers=headers)
    return JSONResponse(_rpc_body(req_id, result), headers=headers)


def _cited_ok(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """A successful, CITED tool result (mirrors mcp-eng-memory's structuredContent shape)."""
    payload = {
        "tool": name,
        "output": {"echoed_args": args},
        "citations": [{"kind": "entity", "title": args.get("target") or name}],
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload,
        "isError": False,
    }


def make_mcp_server(
    *,
    server_name: str = "mcp-eng-memory",
    respond_sse: bool = False,
    session_id: str | None = None,
    tools_call_status: int = 200,
    tool_fn: Any = None,
    calls: list[dict[str, Any]] | None = None,
) -> Starlette:
    """Build a real Starlette MCP server speaking JSON-RPC 2.0 over Streamable-HTTP.

    ``calls`` (if given) records every message the server received (method + params + the
    identity/idempotency/session headers) so a test can assert what actually rode the wire.
    ``tool_fn(name, args) -> result-dict`` customises the tools/call result (default: a cited
    echo). ``tools_call_status != 200`` makes tools/call return that HTTP status (to exercise
    the client's transport-level 4xx/5xx handling). ``session_id`` makes initialize advertise
    an ``Mcp-Session-Id`` the client must echo back on tools/call.
    """
    recorded = calls if calls is not None else []
    resolve_tool = tool_fn or _cited_ok

    async def endpoint(request: Request) -> Response:
        msg = json.loads(await request.body())
        method = msg.get("method")
        req_id = msg.get("id")
        recorded.append(
            {
                "method": method,
                "params": msg.get("params"),
                "authorization": request.headers.get("authorization"),
                "x_forwarded_agent_jwt": request.headers.get("x-forwarded-agent-jwt"),
                "idempotency_key": request.headers.get("idempotency-key"),
                "mcp_session_id": request.headers.get("mcp-session-id"),
                "accept": request.headers.get("accept"),
            }
        )
        if method == "initialize":
            headers = {"mcp-session-id": session_id} if session_id else {}
            return _rpc_response(
                req_id,
                {
                    "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": server_name, "version": "1.0.0"},
                },
                sse=respond_sse,
                headers=headers,
            )
        if method == "tools/call":
            if tools_call_status != 200:
                return JSONResponse({"error": "boom"}, status_code=tools_call_status)
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            return _rpc_response(req_id, resolve_tool(name, args), sse=respond_sse, headers={})
        if method == "tools/list":
            return _rpc_response(req_id, {"tools": []}, sse=respond_sse, headers={})
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown method"}}
        )

    return Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])


# ════════════════════════════ Fakes for the OTHER two clients ════════════════════════════
class _FakeTokens:
    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        return SVC_JWT

    async def aclose(self) -> None:
        return None


@dataclass
class _FakeRegistry:
    """resolve_tool -> a ToolResolution whose manifest points at the in-process server;
    get_tool_access -> the scripted per-(server, capability) mode (default automated)."""

    manifests: dict[str, dict[str, Any]]
    versions: dict[str, str] = field(default_factory=dict)
    access: dict[tuple[str, str | None], Any] = field(default_factory=dict)
    resolve_calls: list[tuple[str, str | None]] = field(default_factory=list)
    access_calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def resolve_tool(self, name: str, version: str | None = None, **kw: Any) -> Any:
        self.resolve_calls.append((name, version))
        from agent_runtime.services.registry_client import ToolResolution

        return ToolResolution(
            name=name,
            version=self.versions.get(name, version or "1.0.0"),
            manifest=self.manifests[name],
            invoke_url=INVOKE_URL,
        )

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
class _FakeLlms:
    completions: list[ChatCompletion]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    offered: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ChatCompletion:
        self.calls.append([dict(m) for m in messages])
        self.offered.append(kw.get("tools") or [])
        return self.completions.pop(0)


# ── metered-outbox recording pool (identical shape to test_wp12_tool_loop) ─────────────
@dataclass
class _RecordingPool:
    inserts: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def connection(self) -> Any:
        return _RecordingConn(self)


class _AsyncNullCtx:
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
        if "INSERT INTO xagent.outbox" in sql:
            self._pool.inserts.append((sql, params))
        return None


# ════════════════════════════ Builders ════════════════════════════
def _manifest(server_name: str, tools: list[str]) -> dict[str, Any]:
    return {
        "name": server_name,
        "description": f"{server_name} server",
        "tools": [
            {
                "name": t,
                "description": f"the {t} tool",
                "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}},
            }
            for t in tools
        ],
        "mcp": {"transport": "streamable-http", "endpoint": "/mcp"},
    }


def _completion(*, content: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatCompletion:
    return ChatCompletion(
        content=content,
        finish_reason="tool_calls" if tool_calls else "stop",
        model="smart",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
        tool_calls=tool_calls or [],
        raw={},
    )


def _settings(**overrides: Any) -> Settings:
    base = get_settings().model_dump()
    base.update(overrides)
    return Settings(**base)


def _real_mcp(app: Starlette, **settings_overrides: Any) -> tuple[McpClient, httpx.AsyncClient]:
    """A REAL McpClient whose httpx client is transported straight into the in-process app."""
    http = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://mcp-server")
    settings = _settings(mcp_retry_attempts=0, **settings_overrides)
    return McpClient(settings, _FakeTokens(), client=http), http


def _agent(allowed_tools: list[str], *, tool_loop_enabled: bool = True) -> AgentRuntime:
    return AgentRuntime(
        agent_id=AGENT, tenant_id=TENANT, name="A", system_prompt="s",
        llm_model="smart", allowed_tools=allowed_tools, tool_loop_enabled=tool_loop_enabled,
    )


def _ctx(agent: AgentRuntime, *, pool: Any = None, cost_budget: float | None = None) -> PipelineContext:
    return PipelineContext(
        principal=Principal(tenant_id=TENANT, agent_id=AGENT, scopes=["agent:execute"], raw_token="jwt"),
        inbound_agent_jwt=AGENT_JWT, trace_id=TRACE_ID, request_id="req-1",
        task=TaskRow(task_id=TASK_ID, agent_id=AGENT, tenant_id=TENANT, trace_id=TRACE_ID,
                     status="running", input={"message": "who owns acme/payments?"}),
        agent=agent, prompt_text="who owns acme/payments?",
        messages=[{"role": "user", "content": "who owns acme/payments?"}],
        steps=StepBuffer(), pool=pool, started_monotonic=time.monotonic(), cost_budget_usd=cost_budget,
    )


def _tool_steps(ctx: PipelineContext) -> list[Any]:
    return [s for s in ctx.steps.steps if s.step_type == STEP_TYPE_TOOL_CALL]


@pytest.fixture(autouse=True)
def _unwire() -> Any:
    yield
    deps.set_enhancement_clients()
    deps.set_clients(guardrails_client=None, llms_client=None)


# ════════════════════════════ Scenario 1 — happy path, real wire ════════════════════════════
async def test_happy_path_llm_calls_tool_via_real_mcp() -> None:
    calls: list[dict[str, Any]] = []
    app = make_mcp_server(calls=calls)
    registry = _FakeRegistry(manifests={"who_owns": _manifest("mcp-eng-memory", ["who_owns"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[
            ToolCall(id="call-1", name="who_owns", arguments={"target": "acme/payments"}),
        ]),
        _completion(content="acme/payments is owned by the payments team."),
    ])
    pool = _RecordingPool()
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["who_owns"]), pool=pool)

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    # (a) The tool was offered to the LLM under its capability name.
    assert [t["function"]["name"] for t in llms.offered[0]] == ["who_owns"]

    # (b) The REAL McpClient did the real two-message handshake against the REAL server.
    methods = [c["method"] for c in calls]
    assert methods == ["initialize", "tools/call"]

    # (c) The tools/call carried the correct name + arguments + identity + idempotency (Contract 9/12/13).
    tool_call = calls[1]
    assert tool_call["params"] == {"name": "who_owns", "arguments": {"target": "acme/payments"}}
    assert tool_call["idempotency_key"] == f"{TASK_ID}:call-1"
    assert tool_call["authorization"] == f"Bearer {SVC_JWT}"
    assert tool_call["x_forwarded_agent_jwt"] == AGENT_JWT

    # (d) The server's REAL cited result was fed back to the LLM as a role:"tool" message.
    second_turn = llms.calls[1]
    tool_msg = next(m for m in second_turn if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "call-1"
    fed_back = json.loads(tool_msg["content"])
    assert fed_back["result"]["tool"] == "who_owns"
    assert fed_back["result"]["citations"], "the real server's citations must survive the round-trip"
    assert fed_back["result"]["output"] == {"echoed_args": {"target": "acme/payments"}}

    # (e) The LLM's final answer stands; access was checked; one metered outbox row; one passed step.
    assert ctx.final_answer == "acme/payments is owned by the payments team."
    assert registry.access_calls == [("who_owns", "who_owns")]
    assert len(pool.inserts) == 1
    steps = _tool_steps(ctx)
    assert len(steps) == 1 and steps[0].status == "passed"
    assert ctx.terminal_error is None


# ════════════════════════════ Scenario 2 — SSE (Streamable-HTTP) response body ════════════════════════════
async def test_sse_response_body_is_parsed_over_real_wire() -> None:
    app = make_mcp_server(respond_sse=True)
    registry = _FakeRegistry(manifests={"who_owns": _manifest("s", ["who_owns"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="who_owns", arguments={"target": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["who_owns"]))

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    # The text/event-stream `data:` frame was parsed into the same result and fed back.
    tool_msg = next(m for m in llms.calls[1] if m.get("role") == "tool")
    assert json.loads(tool_msg["content"])["result"]["output"] == {"echoed_args": {"target": "x"}}
    assert ctx.final_answer == "done"
    assert _tool_steps(ctx)[0].status == "passed"


# ════════════════════════════ Scenario 3 — initialize session handshake ════════════════════════════
async def test_session_id_from_initialize_is_echoed_on_tools_call() -> None:
    calls: list[dict[str, Any]] = []
    app = make_mcp_server(session_id="sess-abc-123", calls=calls)
    registry = _FakeRegistry(manifests={"who_owns": _manifest("s", ["who_owns"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="who_owns", arguments={"target": "x"})]),
        _completion(content="done"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)

    try:
        await ToolLoopStage().run(_ctx(_agent(["who_owns"])))
    finally:
        await http.aclose()

    # initialize advertised the session; the client threaded it onto the tools/call POST.
    init_msg, call_msg = calls[0], calls[1]
    assert init_msg["method"] == "initialize"
    assert call_msg["method"] == "tools/call"
    assert call_msg["mcp_session_id"] == "sess-abc-123"


# ════════════════════════════ Scenario 4 — isError (retryable) fail-soft ════════════════════════════
async def test_tool_iserror_retryable_is_fail_soft() -> None:
    def _err(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "provider down"}],
            "isError": True,
            "_meta": {"code": "SERVICE_UNAVAILABLE", "retryable": True},
        }

    app = make_mcp_server(tool_fn=_err)
    registry = _FakeRegistry(manifests={"flaky": _manifest("s", ["flaky"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="flaky", arguments={"target": "x"})]),
        _completion(content="answered without the tool"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["flaky"]))

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    # The error was fed back (not fatal); the LLM still produced a final answer; step failed.
    tool_msg = next(m for m in llms.calls[1] if m.get("role") == "tool")
    assert "error" in json.loads(tool_msg["content"])
    assert ctx.final_answer == "answered without the tool"
    assert ctx.terminal_error is None
    assert _tool_steps(ctx)[0].status == "failed"


# ════════════════════════════ Scenario 5 — isError (terminal) fail-soft ════════════════════════════
async def test_tool_iserror_terminal_is_fail_soft() -> None:
    def _err(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "bad args"}],
            "isError": True,
            "_meta": {"code": "VALIDATION_ERROR", "retryable": False},
        }

    app = make_mcp_server(tool_fn=_err)
    registry = _FakeRegistry(manifests={"picky": _manifest("s", ["picky"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="picky", arguments={"target": "x"})]),
        _completion(content="recovered"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["picky"]))

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    assert ctx.final_answer == "recovered"
    assert ctx.terminal_error is None
    assert _tool_steps(ctx)[0].status == "failed"


# ════════════════════════════ Scenario 6 — HTTP 4xx from the server, fail-soft ════════════════════════════
async def test_tool_http_4xx_is_fail_soft() -> None:
    app = make_mcp_server(tools_call_status=400)
    registry = _FakeRegistry(manifests={"who_owns": _manifest("s", ["who_owns"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="who_owns", arguments={"target": "x"})]),
        _completion(content="handled the 4xx"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["who_owns"]))

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    assert ctx.final_answer == "handled the 4xx"
    assert ctx.terminal_error is None
    assert _tool_steps(ctx)[0].status == "failed"


# ── Scenario 7 — multi-tool dispatch over the real wire ──
async def test_multi_tool_dispatch_over_real_wire() -> None:
    calls: list[dict[str, Any]] = []
    app = make_mcp_server(server_name="mcp-suite", calls=calls)
    # One aggregating server manifest hosting two member tools.
    registry = _FakeRegistry(manifests={"mcp-suite": _manifest("mcp-suite", ["who_owns", "what_breaks"])})
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[
            ToolCall(id="a", name="who_owns", arguments={"target": "acme"}),
            ToolCall(id="b", name="what_breaks", arguments={"target": "acme/db"}),
        ]),
        _completion(content="both tools answered"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["mcp-suite"]))

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    # The single server entry resolved ONCE and offered BOTH members.
    assert registry.resolve_calls == [("mcp-suite", None)]
    assert sorted(t["function"]["name"] for t in llms.offered[0]) == ["what_breaks", "who_owns"]
    # Both members were dispatched over the real wire, routed by name, in order.
    tool_calls = [c for c in calls if c["method"] == "tools/call"]
    assert [c["params"]["name"] for c in tool_calls] == ["who_owns", "what_breaks"]
    # Both results fed back; both steps passed; final answer stands.
    assert [s.status for s in _tool_steps(ctx)] == ["passed", "passed"]
    assert ctx.final_answer == "both tools answered"


# ── Scenario 8 — access-denied never reaches the server ──
async def test_access_denied_short_circuits_before_any_real_invoke() -> None:
    calls: list[dict[str, Any]] = []
    app = make_mcp_server(calls=calls)
    registry = _FakeRegistry(
        manifests={"who_owns": _manifest("s", ["who_owns"])},
        access={("who_owns", "who_owns"): "none"},
    )
    real_mcp, http = _real_mcp(app)
    llms = _FakeLlms(completions=[
        _completion(tool_calls=[ToolCall(id="c1", name="who_owns", arguments={"target": "x"})]),
        _completion(content="not allowed, answered directly"),
    ])
    deps.set_enhancement_clients(registry_client=registry, mcp_client=real_mcp)
    deps.set_clients(guardrails_client=None, llms_client=llms)
    ctx = _ctx(_agent(["who_owns"]))

    try:
        await ToolLoopStage().run(ctx)
    finally:
        await http.aclose()

    # Denied BEFORE dispatch: the real server saw NOTHING (not even initialize).
    assert calls == []
    denied_msg = next(m for m in llms.calls[1] if m.get("role") == "tool")
    assert json.loads(denied_msg["content"])["error"] == "tool_access_denied"
    assert _tool_steps(ctx)[0].status == "failed"
    assert ctx.final_answer == "not allowed, answered directly"
