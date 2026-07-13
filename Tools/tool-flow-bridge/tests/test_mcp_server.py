"""POST /w/{slug}/mcp — real-MCP (JSON-RPC 2.0 / Streamable HTTP) for a flow-tool.

The legacy single-tool wire is now a BACK-COMPAT ALIAS that resolves the flow-tool's SINGLETON
MCP from the new source-of-truth model (server_name ``tool-<slug>``) and dispatches through the
SAME aggregating handler as ``/m``. These tests drive that alias: lifecycle, discovery,
invocation, and the access-grant / schema / idempotency governance still hold, and the singleton's
serverInfo.name stays ``tool-<slug>`` (byte-for-byte back-compat).
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_TENANT, make_principal
from tool_flow_bridge.api import mcp as mcp_api
from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries
from tool_flow_bridge.services import mcp_protocol

SLUG = "sum-tool"

# The singleton MCP that backs the flow-tool's slug: slug == the flow-tool slug, server_name is the
# preserved registry key ``tool-<slug>``, and it wraps exactly the one atomic tool 'add'.
MCP_ROW = {
    "mcp_id": "mcp-sum",
    "tenant_id": TEST_TENANT,
    "slug": SLUG,
    "server_name": f"tool-{SLUG}",
    "display_name": "Sum Tool",
    "description": "Adds two numbers.",
    "visibility": "private",
    "status": "active",
    "version": "1.0.0",
}
TOOL_ROW = {
    "tool_id": "tid-add",
    "tenant_id": TEST_TENANT,
    "status": "active",
    "slug": SLUG,
    "snake_name": "add",
    "display_name": "Sum Tool",
    "description": "Adds two numbers.",
    "version": "1.0.0",
    "internal_host": "http://nodered:1880",
    "http_node_root": "/flow",
    "http_path": "/sum",
    "http_method": "POST",
    "invoke_secret_ref": "static:invoke",
    "input_schema": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    "output_schema": {"type": "object", "properties": {"sum": {"type": "integer"}}},
}


def _rpc(method: str, params: dict | None = None, msg_id: int | str | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


@pytest.fixture
def wire(monkeypatch):
    """Patch the DB helpers + queries + Node-RED adapter (bound in the mcp module) + access grant."""

    async def fake_in_tenant(pool, tenant_id, fn):
        return await fn(None)

    async def fake_mcp(conn, slug):
        return (MCP_ROW, [TOOL_ROW]) if slug == SLUG else None

    calls = {"n": 0}

    async def fake_invoke_workflow(client, **kw):
        calls["n"] += 1
        return {"sum": kw["args"]["a"] + kw["args"]["b"]}

    async def fake_access(*_a, **_k):
        return "automated"

    monkeypatch.setattr(db_pool, "in_tenant", fake_in_tenant)
    monkeypatch.setattr(queries, "get_mcp_with_members", fake_mcp)
    monkeypatch.setattr(mcp_api, "invoke_workflow", fake_invoke_workflow)  # bound in the mcp module
    monkeypatch.setattr(mcp_api, "_resolve_tool_access", fake_access)
    return calls


# ── Lifecycle + discovery ───────────────────────────────────────────────────────
async def test_initialize(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(f"/w/{SLUG}/mcp", json=_rpc("initialize", {"protocolVersion": "2025-06-18"}))
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == f"tool-{SLUG}"


async def test_tools_list(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(f"/w/{SLUG}/mcp", json=_rpc("tools/list"))
    tools = resp.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["add"]
    assert tools[0]["inputSchema"]["required"] == ["a", "b"]


# ── Invocation ──────────────────────────────────────────────────────────────────
async def test_tools_call_happy_path(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/w/{SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"sum": 5}
    assert wire["n"] == 1


async def test_tools_call_unknown_tool_name(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(f"/w/{SLUG}/mcp", json=_rpc("tools/call", {"name": "nope", "arguments": {}}))
    assert resp.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS


async def test_tools_call_unknown_slug(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post("/w/nope/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {}}))
    assert resp.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS


async def test_tools_call_access_denied(make_client, wire, monkeypatch) -> None:
    async def deny(*_a, **_k):
        return "none"

    monkeypatch.setattr(mcp_api, "_resolve_tool_access", deny)
    ac = await make_client()
    resp = await ac.post(
        f"/w/{SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert result["_meta"]["code"] == "FORBIDDEN"
    assert wire["n"] == 0


async def test_tools_call_schema_violation(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/w/{SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": "x", "b": 3}})
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert result["_meta"]["code"] == "VALIDATION_ERROR"
    assert result["_meta"]["pointer"] == "/a"


async def test_tools_call_missing_coarse_scope(make_client, wire) -> None:
    # No tool:invoke -> the coarse gate rejects at the HTTP layer (before JSON-RPC dispatch).
    ac = await make_client(principal=make_principal(["tenant:read"]))
    resp = await ac.post(
        f"/w/{SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
    )
    assert resp.status_code == 403


async def test_tools_call_idempotent_replay(make_client, wire) -> None:
    ac = await make_client()
    call = _rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 2}})
    headers = {"Idempotency-Key": "abc-123"}
    first = await ac.post(f"/w/{SLUG}/mcp", json=call, headers=headers)
    second = await ac.post(f"/w/{SLUG}/mcp", json=call, headers=headers)
    assert first.json()["result"]["structuredContent"] == {"sum": 4}
    assert second.json()["result"]["structuredContent"] == {"sum": 4}
    # The workflow ran only once; the second call replayed the cached result.
    assert wire["n"] == 1


# ── Protocol edges ──────────────────────────────────────────────────────────────
async def test_notification_returns_202(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(f"/w/{SLUG}/mcp", json=_rpc("notifications/initialized", msg_id=None))
    assert resp.status_code == 202


async def test_unknown_method(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(f"/w/{SLUG}/mcp", json=_rpc("resources/list"))
    assert resp.json()["error"]["code"] == mcp_protocol.METHOD_NOT_FOUND
