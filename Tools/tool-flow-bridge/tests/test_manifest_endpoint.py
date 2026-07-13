"""GET /w/{slug}/manifest tests (unauthenticated; DB monkeypatched)."""

from __future__ import annotations

import pytest

from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries

SLUG = "sum-tool-aabbccdd"
# The manifest endpoint REGENERATES the manifest from the persisted singleton MCP + member rows
# (a live projection), so the fixture provides the stored rows, not a frozen manifest. The
# singleton's server_name stays ``tool-<slug>`` (the preserved registry key).
MCP_ROW = {
    "mcp_id": "mcp-sum",
    "tenant_id": "00000000-0000-0000-0000-0000000000aa",
    "slug": SLUG,
    "server_name": f"tool-{SLUG}",
    "display_name": "Sum tool",
    "description": "Sum tool.",
    "visibility": "private",
    "status": "active",
    "version": "1.0.0",
}
TOOL_ROW = {
    "snake_name": "add",
    "display_name": "Sum tool",
    "description": "Sum tool.",
    "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
    "output_schema": None,
}


@pytest.fixture
def wire(monkeypatch):
    async def fake_in_platform(pool, fn):
        return await fn(None)

    async def fake_mcp(conn, slug):
        return (MCP_ROW, [TOOL_ROW]) if slug == SLUG else None

    monkeypatch.setattr(db_pool, "in_platform", fake_in_platform)
    monkeypatch.setattr(queries, "get_mcp_with_members", fake_mcp)


async def test_manifest_ok_and_etag(make_client, wire):
    client = await make_client()
    resp = await client.get(f"/w/{SLUG}/manifest")
    assert resp.status_code == 200
    assert resp.json()["name"] == f"tool-{SLUG}"
    etag = resp.headers["ETag"]
    assert etag

    # If-None-Match with the same ETag -> 304.
    resp304 = await client.get(f"/w/{SLUG}/manifest", headers={"If-None-Match": etag})
    assert resp304.status_code == 304


async def test_manifest_unknown_slug(make_client, wire):
    client = await make_client()
    resp = await client.get("/w/does-not-exist/manifest")
    assert resp.status_code == 404
