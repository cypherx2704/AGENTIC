"""POST /m/{mcp_slug}/mcp — the AGGREGATING MCP wire (a collection of atomic tools as one server).

Proves the multi-tool contract on top of the SAME governed invoke pipeline as the single-tool
wire: ``tools/list`` surfaces every member, ``tools/call {name}`` routes by ``snake_name`` to the
right member (unknown name -> INVALID_PARAMS), a tool may belong to two MCPs, and the access-grant
key is the MCP's ``server_name`` + the member's ``name``.
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_TENANT, make_principal
from tool_flow_bridge.api import mcp as mcp_api
from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries
from tool_flow_bridge.services import mcp_protocol


def _tool(snake_name: str, http_path: str, keys: tuple[str, str]) -> dict:
    a, b = keys
    return {
        "tool_id": f"tid-{snake_name}",
        "tenant_id": TEST_TENANT,
        "snake_name": snake_name,
        "display_name": snake_name.title(),
        "description": f"{snake_name} two numbers.",
        "status": "active",
        "version": "1.0.0",
        "http_method": "POST",
        "http_path": http_path,
        "internal_host": "http://nodered:1880",
        "http_node_root": "/flow",
        "invoke_secret_ref": "static:invoke",
        "input_schema": {
            "type": "object",
            "properties": {a: {"type": "integer"}, b: {"type": "integer"}},
            "required": [a, b],
            "additionalProperties": False,
        },
        "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
    }


# A multi-tool MCP: server "mcp-math-aabbccdd" exposing add + mul.
MATH_SLUG = "mcp-math-aabbccdd"
MATH_MCP = {
    "mcp_id": "mcp-math",
    "tenant_id": TEST_TENANT,
    "slug": MATH_SLUG,
    "server_name": MATH_SLUG,
    "display_name": "Math",
    "description": "Math tools.",
    "visibility": "private",
    "status": "active",
    "version": "1.2.0",
}
ADD = _tool("add", "/add", ("a", "b"))
MUL = _tool("mul", "/mul", ("x", "y"))

# The SAME tool (add) belongs to a second MCP — proves many-to-many membership.
OTHER_SLUG = "mcp-arith-aabbccdd"
OTHER_MCP = {
    "mcp_id": "mcp-arith",
    "tenant_id": TEST_TENANT,
    "slug": OTHER_SLUG,
    "server_name": OTHER_SLUG,
    "display_name": "Arith",
    "description": "Arithmetic tools.",
    "visibility": "private",
    "status": "active",
    "version": "1.0.0",
}

_MEMBERS = {MATH_SLUG: (MATH_MCP, [ADD, MUL]), OTHER_SLUG: (OTHER_MCP, [ADD])}


def _rpc(method: str, params: dict | None = None, msg_id: int | str | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return msg


@pytest.fixture
def wire(monkeypatch):
    """Patch the DB helpers + queries + Node-RED adapter + access grant (all bound in mcp_api)."""

    async def fake_in_tenant(pool, tenant_id, fn):
        return await fn(None)

    async def fake_in_platform(pool, fn):
        return await fn(None)

    async def fake_get_mcp(conn, slug):
        return _MEMBERS.get(slug)

    calls: dict[str, list] = {"invoked": []}

    async def fake_invoke_workflow(client, **kw):
        calls["invoked"].append(kw["http_path"])
        vals = list(kw["args"].values())
        return {"value": vals[0] + vals[1]}

    async def fake_access(*_a, **_k):
        return "automated"

    monkeypatch.setattr(db_pool, "in_tenant", fake_in_tenant)
    monkeypatch.setattr(db_pool, "in_platform", fake_in_platform)
    monkeypatch.setattr(queries, "get_mcp_with_members", fake_get_mcp)
    monkeypatch.setattr(mcp_api, "invoke_workflow", fake_invoke_workflow)
    monkeypatch.setattr(mcp_api, "_resolve_tool_access", fake_access)
    return calls


# ── Lifecycle + discovery ───────────────────────────────────────────────────────
async def test_initialize_uses_server_name(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("initialize", {"protocolVersion": "2025-06-18"})
    )
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == MATH_MCP["server_name"]


async def test_tools_list_shows_all_members(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/list"))
    tools = resp.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["add", "mul"]
    assert tools[0]["inputSchema"]["required"] == ["a", "b"]
    assert tools[1]["outputSchema"] == MUL["output_schema"]


# ── Routing by name ──────────────────────────────────────────────────────────────
async def test_tools_call_routes_add(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"value": 5}
    assert wire["invoked"] == ["/add"]  # routed to add's binding, not mul's


async def test_tools_call_routes_mul(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/call", {"name": "mul", "arguments": {"x": 4, "y": 5}})
    )
    assert resp.json()["result"]["structuredContent"] == {"value": 9}
    assert wire["invoked"] == ["/mul"]


async def test_tools_call_unknown_name(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/call", {"name": "nope", "arguments": {}})
    )
    assert resp.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS
    assert wire["invoked"] == []


async def test_tools_call_unknown_mcp(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        "/m/does-not-exist/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {}})
    )
    assert resp.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS


# ── Many-to-many: the same tool reached through a second MCP ──────────────────────
async def test_tool_in_two_mcps(make_client, wire) -> None:
    ac = await make_client()
    # add is a member of BOTH mcp-math and mcp-arith; both route to the same binding.
    via_math = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
    )
    via_arith = await ac.post(
        f"/m/{OTHER_SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 7, "b": 1}})
    )
    assert via_math.json()["result"]["structuredContent"] == {"value": 2}
    assert via_arith.json()["result"]["structuredContent"] == {"value": 8}
    # arith exposes only add:
    listing = await ac.post(f"/m/{OTHER_SLUG}/mcp", json=_rpc("tools/list"))
    assert [t["name"] for t in listing.json()["result"]["tools"]] == ["add"]


# ── Governance parity with the single-tool wire ──────────────────────────────────
async def test_access_denied_blocks_invoke(make_client, wire, monkeypatch) -> None:
    async def deny(*_a, **_k):
        return "none"

    monkeypatch.setattr(mcp_api, "_resolve_tool_access", deny)
    ac = await make_client()
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert result["_meta"]["code"] == "FORBIDDEN"
    assert wire["invoked"] == []


async def test_schema_violation(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp",
        json=_rpc("tools/call", {"name": "add", "arguments": {"a": "x", "b": 3}}),
    )
    result = resp.json()["result"]
    assert result["isError"] is True
    assert result["_meta"]["code"] == "VALIDATION_ERROR"
    assert result["_meta"]["pointer"] == "/a"


async def test_missing_coarse_scope(make_client, wire) -> None:
    ac = await make_client(principal=make_principal(["tenant:read"]))
    resp = await ac.post(
        f"/m/{MATH_SLUG}/mcp", json=_rpc("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}})
    )
    assert resp.status_code == 403


async def test_idempotent_replay(make_client, wire) -> None:
    ac = await make_client()
    call = _rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 2}})
    headers = {"Idempotency-Key": "agg-1"}
    first = await ac.post(f"/m/{MATH_SLUG}/mcp", json=call, headers=headers)
    second = await ac.post(f"/m/{MATH_SLUG}/mcp", json=call, headers=headers)
    assert first.json()["result"]["structuredContent"] == {"value": 4}
    assert second.json()["result"]["structuredContent"] == {"value": 4}
    assert wire["invoked"] == ["/add"]  # ran once; second replayed


# ── Aggregating manifest endpoint (unauth, ETag) ─────────────────────────────────
async def test_aggregating_manifest_and_etag(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.get(f"/m/{MATH_SLUG}/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == MATH_MCP["server_name"]
    assert body["visibility"] == "private"
    assert [t["name"] for t in body["tools"]] == ["add", "mul"]
    assert body["base_url"].endswith(f"/m/{MATH_SLUG}")
    etag = resp.headers["ETag"]
    assert etag
    resp304 = await ac.get(f"/m/{MATH_SLUG}/manifest", headers={"If-None-Match": etag})
    assert resp304.status_code == 304


async def test_aggregating_manifest_unknown(make_client, wire) -> None:
    ac = await make_client()
    resp = await ac.get("/m/does-not-exist/manifest")
    assert resp.status_code == 404
