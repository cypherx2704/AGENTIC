"""Tests for POST /v1/flow-tools/{slug}/test (owner-only run-with-sample-args)."""

from __future__ import annotations

import pytest

from tests.conftest import make_principal
from tool_flow_bridge.db import pool as db_pool
from tool_flow_bridge.db import queries
from tool_flow_bridge.services import publisher as pub

SLUG = "sum-tool"
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
    async def fake_in_tenant(pool, tenant_id, fn):
        return await fn(None)

    async def fake_binding(conn, slug):
        return BINDING if slug == SLUG else None

    async def fake_invoke_workflow(client, **kw):
        return {"sum": kw["args"]["a"] + kw["args"]["b"]}

    monkeypatch.setattr(db_pool, "in_tenant", fake_in_tenant)
    monkeypatch.setattr(queries, "get_binding_with_runtime", fake_binding)
    monkeypatch.setattr(pub, "invoke_workflow", fake_invoke_workflow)


async def test_test_tool_runs(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke", "tool:admin"]))
    resp = await client.post(f"/v1/flow-tools/{SLUG}/test", json={"args": {"a": 4, "b": 5}})
    assert resp.status_code == 200
    assert resp.json() == {"tool": "add", "result": {"sum": 9}}


async def test_test_tool_requires_admin(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke"]))
    resp = await client.post(f"/v1/flow-tools/{SLUG}/test", json={"args": {"a": 1, "b": 1}})
    assert resp.status_code == 403


async def test_test_tool_validates_args(make_client, wire):
    client = await make_client(principal=make_principal(["tool:invoke", "tool:admin"]))
    resp = await client.post(f"/v1/flow-tools/{SLUG}/test", json={"args": {"a": "x", "b": 1}})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["pointer"] == "/a"
