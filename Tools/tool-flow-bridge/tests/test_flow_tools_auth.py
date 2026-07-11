"""Publish authorization tests — the scope gates fire before any DB/registry work."""

from __future__ import annotations

from tests.conftest import make_principal


def _body(access_mode: str) -> dict:
    return {
        "node_red_flow_id": "x",
        "tool": {"title": "T", "description": "d", "access_mode": access_mode},
    }


async def test_publish_requires_tool_admin(make_client):
    # Only tool:invoke -> not tool:admin -> 403 before the publisher runs.
    client = await make_client(principal=make_principal(["tool:invoke"]))
    resp = await client.post("/v1/flow-tools", json=_body("automated"))
    assert resp.status_code == 403


async def test_publish_ask_requires_tenant_admin(make_client):
    # Has tool:admin but a non-automated (ask) default also needs tenant:admin -> 403.
    client = await make_client(principal=make_principal(["tool:invoke", "tool:admin"]))
    resp = await client.post("/v1/flow-tools", json=_body("ask"))
    assert resp.status_code == 403
