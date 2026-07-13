"""Phase 5 · cross-tenant PUBLIC invoke — a promoted (public) MCP resolves + invokes from a FOREIGN
tenant's context, proving the migration-0007 ``_public_read`` RLS widening plus promote's member-tool
visibility flip close the cross-tenant Public-execution gap at the resolution layer.

The unit suite has NO live Postgres, so RLS is EMULATED in the fake ``get_mcp_with_members``: a row is
visible to the calling tenant iff it is OWNED by that tenant OR it is ``visibility='public'`` — exactly
the permissive-OR of the own ``_read`` policy (0004) and the new ``_public_read`` policy (0007). The
ACTUAL cross-tenant RLS SELECT can only be validated against a live Postgres — the operator's step
(see ``db/migrations/20260712_0007__public_mcp_read.sql``).
"""

from __future__ import annotations

import pytest

from tool_flow_bridge.api import mcp as mcp_api
from tool_flow_bridge.core.auth import Principal
from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries
from tool_flow_bridge.services import mcp_protocol

OWNER_TENANT = "00000000-0000-0000-0000-0000000000cc"
CALLER_TENANT = "00000000-0000-0000-0000-0000000000dd"  # a DIFFERENT tenant than the owner


def _tool(snake_name: str, visibility: str) -> dict:
    return {
        "tool_id": f"tid-{snake_name}",
        "tenant_id": OWNER_TENANT,
        "snake_name": snake_name,
        "display_name": snake_name.title(),
        "description": f"{snake_name} a number.",
        "status": "active",
        "version": "1.0.0",
        "visibility": visibility,
        "http_method": "POST",
        "http_path": f"/{snake_name}",
        # After promote the public tool is re-homed onto the platform runtime (sentinel row,
        # readable in any context per migration 0006).
        "internal_host": "http://nodered-platform:1880",
        "http_node_root": "/flow",
        "invoke_secret_ref": "static:invoke",
        "input_schema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        "output_schema": None,
    }


PUBLIC_SLUG = "mcp-public"
PUBLIC_MCP = {
    "mcp_id": "mcp-public-id", "tenant_id": OWNER_TENANT, "slug": PUBLIC_SLUG,
    "server_name": "mcp-public", "display_name": "Public", "description": "Public tools.",
    "visibility": "public", "status": "active", "version": "1.1.0",
}
PUBLIC_TOOL = _tool("echo", "public")

PRIVATE_SLUG = "mcp-private-00000000"
PRIVATE_MCP = {
    "mcp_id": "mcp-private-id", "tenant_id": OWNER_TENANT, "slug": PRIVATE_SLUG,
    "server_name": "mcp-private-00000000", "display_name": "Private",
    "description": "Private tools.", "visibility": "private", "status": "active", "version": "1.0.0",
}
PRIVATE_TOOL = _tool("secret", "private")

_STORE = {
    PUBLIC_SLUG: (PUBLIC_MCP, [PUBLIC_TOOL]),
    PRIVATE_SLUG: (PRIVATE_MCP, [PRIVATE_TOOL]),
}


def _principal(tenant_id: str) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        agent_id="00000000-0000-0000-0000-0000000000bb",
        scopes=["tool:invoke"],
        principal_type="agent",
    )


def _call(name: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": {"a": 1}}}


@pytest.fixture
def cross_tenant_wire(monkeypatch):
    """Patch the invoke wire with a tenant-AWARE fake that emulates the own-OR-public RLS boundary."""
    seen: dict[str, str | None] = {"tenant": None}

    async def fake_in_tenant(pool, tenant_id, fn):
        seen["tenant"] = tenant_id  # the CALLING tenant's GUC
        return await fn(None)

    async def fake_in_platform(pool, fn):
        return await fn(None)

    async def fake_get_mcp(conn, slug):
        entry = _STORE.get(slug)
        if entry is None:
            return None
        mcp, members = entry
        caller = seen["tenant"]
        # Emulate RLS permissive-OR: own-tenant (_read, 0004) OR public (_public_read, 0007).
        if mcp["tenant_id"] != caller and mcp["visibility"] != "public":
            return None
        visible = [m for m in members if m["tenant_id"] == caller or m["visibility"] == "public"]
        return mcp, visible

    calls: dict[str, list] = {"invoked": []}

    async def fake_invoke_workflow(client, **kw):
        calls["invoked"].append(kw["http_path"])
        return {"ok": list(kw["args"].values())[0]}

    async def fake_access(*_a, **_k):
        return "automated"

    monkeypatch.setattr(db_pool, "in_tenant", fake_in_tenant)
    monkeypatch.setattr(db_pool, "in_platform", fake_in_platform)
    monkeypatch.setattr(queries, "get_mcp_with_members", fake_get_mcp)
    monkeypatch.setattr(mcp_api, "invoke_workflow", fake_invoke_workflow)
    monkeypatch.setattr(mcp_api, "_resolve_tool_access", fake_access)
    return calls


# ── the fix: a foreign tenant resolves + invokes a PUBLIC MCP ─────────────────────
async def test_public_mcp_invokes_cross_tenant(make_client, cross_tenant_wire) -> None:
    """CALLER_TENANT (!= OWNER_TENANT) loads the public MCP + its public member and invokes it —
    _load_mcp + the governed invoke succeed with NO owner-only gating."""
    ac = await make_client(principal=_principal(CALLER_TENANT))
    resp = await ac.post(f"/m/{PUBLIC_SLUG}/mcp", json=_call("echo"))
    result = resp.json()["result"]
    assert result["isError"] is False
    assert cross_tenant_wire["invoked"] == ["/echo"]


async def test_public_mcp_tools_list_cross_tenant(make_client, cross_tenant_wire) -> None:
    """tools/list from a foreign tenant surfaces the public member (the member row is public-readable)."""
    ac = await make_client(principal=_principal(CALLER_TENANT))
    resp = await ac.post(
        f"/m/{PUBLIC_SLUG}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    tools = resp.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo"]


# ── negative control: a foreign tenant CANNOT reach a PRIVATE MCP ─────────────────
async def test_private_mcp_not_resolvable_cross_tenant(make_client, cross_tenant_wire) -> None:
    """A private MCP owned by OWNER_TENANT stays invisible/invokable-forbidden to a foreign tenant —
    the widening admits ONLY public rows, private stays own-tenant-only."""
    ac = await make_client(principal=_principal(CALLER_TENANT))
    resp = await ac.post(f"/m/{PRIVATE_SLUG}/mcp", json=_call("secret"))
    assert resp.json()["error"]["code"] == mcp_protocol.INVALID_PARAMS
    assert cross_tenant_wire["invoked"] == []


# ── sanity: the OWNER still reaches its own private MCP (own _read policy) ─────────
async def test_owner_still_resolves_own_private_mcp(make_client, cross_tenant_wire) -> None:
    ac = await make_client(principal=_principal(OWNER_TENANT))
    resp = await ac.post(f"/m/{PRIVATE_SLUG}/mcp", json=_call("secret"))
    assert resp.json()["result"]["isError"] is False
    assert cross_tenant_wire["invoked"] == ["/secret"]
