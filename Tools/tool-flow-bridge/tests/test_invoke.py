"""End-to-end invoke-handler tests (DB + Node-RED monkeypatched)."""

from __future__ import annotations

import pytest

from tests.conftest import make_principal
from tool_flow_bridge.api import invoke as invoke_api
from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries

SLUG = "sum-tool"
FINE = f"tool:tool-{SLUG}:invoke"

BINDING = {
    "status": "active",
    "snake_name": "add",
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
}


@pytest.fixture
def wire(monkeypatch):
    """Patch the DB helpers + queries + the Node-RED adapter with in-memory fakes."""

    async def fake_in_tenant(pool, tenant_id, fn):
        return await fn(None)

    async def fake_binding(conn, slug):
        return BINDING if slug == SLUG else None

    calls = {"n": 0}

    async def fake_invoke_workflow(client, **kw):
        calls["n"] += 1
        return {"sum": kw["args"]["a"] + kw["args"]["b"]}

    monkeypatch.setattr(db_pool, "in_tenant", fake_in_tenant)
    monkeypatch.setattr(queries, "get_binding_with_runtime", fake_binding)
    monkeypatch.setattr(invoke_api, "invoke_workflow", fake_invoke_workflow)
    return calls


async def test_invoke_success(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke", FINE]))
    resp = await client.post(f"/w/{SLUG}/mcp/v1/invoke", json={"args": {"a": 2, "b": 3}})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"tool": "add", "result": {"sum": 5}}


async def test_invoke_missing_fine_scope(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke"]))
    resp = await client.post(f"/w/{SLUG}/mcp/v1/invoke", json={"args": {"a": 1, "b": 1}})
    assert resp.status_code == 403


async def test_invoke_unknown_slug(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke", "tool:tool-nope:invoke"]))
    resp = await client.post("/w/nope/mcp/v1/invoke", json={"args": {}})
    assert resp.status_code == 404


async def test_invoke_schema_violation(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke", FINE]))
    resp = await client.post(f"/w/{SLUG}/mcp/v1/invoke", json={"args": {"a": "x", "b": 3}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["pointer"] == "/a"


async def test_invoke_idempotency_replay(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke", FINE]))
    headers = {"Idempotency-Key": "abc-123"}
    r1 = await client.post(f"/w/{SLUG}/mcp/v1/invoke", json={"args": {"a": 2, "b": 2}}, headers=headers)
    r2 = await client.post(f"/w/{SLUG}/mcp/v1/invoke", json={"args": {"a": 2, "b": 2}}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    # The workflow ran only once; the second call replayed the cached result.
    assert wire["n"] == 1
