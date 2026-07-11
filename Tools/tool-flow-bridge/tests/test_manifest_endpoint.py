"""GET /w/{slug}/manifest tests (unauthenticated; DB monkeypatched)."""

from __future__ import annotations

import pytest

from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries

SLUG = "sum-tool-aabbccdd"
MANIFEST = {
    "schema_version": "1.0.0",
    "protocol_version": "mcp/1.0",
    "name": f"tool-{SLUG}",
    "version": "1.0.0",
    "description": "Sum tool.",
    "base_url": f"http://tool-flow-bridge:8080/w/{SLUG}",
    "tools": [{"name": "add", "description": "add", "input_schema": {"type": "object"}}],
}


@pytest.fixture
def wire(monkeypatch):
    async def fake_in_platform(pool, fn):
        return await fn(None)

    async def fake_binding(conn, slug):
        return {"status": "active", "manifest": MANIFEST} if slug == SLUG else None

    monkeypatch.setattr(db_pool, "in_platform", fake_in_platform)
    monkeypatch.setattr(queries, "get_binding_by_slug", fake_binding)


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
