"""By-id GET/PUT/DELETE — immutable-field rejection, owner-only mutation, 404 anti-leak."""

from __future__ import annotations

import pytest

from _helpers import AGENT_A, AGENT_B, bind_principal, make_principal


@pytest.mark.asyncio
async def test_get_own_memory(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    created = await ac.post("/v1/memories", json={"content": "fetch me"})
    mem_id = created.json()["id"]
    g = await ac.get(f"/v1/memories/{mem_id}")
    assert g.status_code == 200
    assert g.json()["id"] == mem_id


@pytest.mark.asyncio
async def test_get_missing_is_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    g = await ac.get("/v1/memories/does-not-exist")
    assert g.status_code == 404


@pytest.mark.asyncio
async def test_put_updates_mutable_fields(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    created = await ac.post("/v1/memories", json={"content": "old", "tags": ["a"]})
    mem_id = created.json()["id"]
    u = await ac.put(f"/v1/memories/{mem_id}",
                     json={"content": "new", "tags": ["b"], "scope": "tenant_shared"})
    assert u.status_code == 200, u.text
    assert u.json()["content"] == "new"
    assert u.json()["tags"] == ["b"]
    assert u.json()["scope"] == "tenant_shared"


@pytest.mark.asyncio
async def test_put_rejects_immutable_fields(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    created = await ac.post("/v1/memories", json={"content": "x"})
    mem_id = created.json()["id"]
    # 'type', 'id', 'principal_id', 'created_at' are not on UpdateMemoryRequest (extra=forbid).
    for field, value in (("type", "fact"), ("id", "abc"), ("principal_id", "z"),
                         ("created_at", "2020-01-01")):
        u = await ac.put(f"/v1/memories/{mem_id}", json={field: value})
        assert u.status_code == 422, (field, u.text)
        assert u.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_put_other_principal_is_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(agent_id=AGENT_B))
    created = await ac.post("/v1/memories", json={"content": "B's", "scope": "tenant_shared"})
    mem_id = created.json()["id"]
    # A cannot mutate B's memory (even a tenant_shared one) -> 404, never 403.
    bind_principal(app, make_principal(agent_id=AGENT_A))
    u = await ac.put(f"/v1/memories/{mem_id}", json={"content": "hijacked"})
    assert u.status_code == 404


@pytest.mark.asyncio
async def test_delete_own_then_gone(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    created = await ac.post("/v1/memories", json={"content": "delete me"})
    mem_id = created.json()["id"]
    d = await ac.delete(f"/v1/memories/{mem_id}")
    assert d.status_code == 200
    assert d.json()["deleted"] is True
    g = await ac.get(f"/v1/memories/{mem_id}")
    assert g.status_code == 404


@pytest.mark.asyncio
async def test_delete_other_principal_is_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(agent_id=AGENT_B))
    created = await ac.post("/v1/memories", json={"content": "B's", "scope": "tenant_shared"})
    mem_id = created.json()["id"]
    bind_principal(app, make_principal(agent_id=AGENT_A))
    d = await ac.delete(f"/v1/memories/{mem_id}")
    assert d.status_code == 404
