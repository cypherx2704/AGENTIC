"""THE scope-visibility matrix — the cross-end-user leak regression guard.

Principal A must NEVER be able to retrieve principal B's ``principal_only`` memories,
under ANY tenant policy. ``tenant_shared`` memories cross ONLY when the tenant's
``user_scope_visibility`` is ``tenant``. We assert this at BOTH layers:

* the pure predicate ``scoping.can_view`` (the single source of truth the SQL mirrors);
* the live search/by-id endpoints (the in-memory repo applies the same predicate).
"""

from __future__ import annotations

import pytest

from _helpers import AGENT_A, AGENT_B, bind_principal, make_principal
from memory_service.services import scoping


# ── The pure predicate: the exhaustive truth table ──────────────────────────────────
@pytest.mark.parametrize("visibility", ["isolated", "tenant"])
def test_principal_only_never_crosses(visibility: str) -> None:
    # B's principal_only memory is invisible to A under EVERY policy.
    assert not scoping.can_view(
        caller_type="agent", caller_id=AGENT_A,
        owner_type="agent", owner_id=AGENT_B,
        memory_scope="principal_only", user_scope_visibility=visibility,
    )


def test_owner_always_sees_own_regardless_of_scope_or_policy() -> None:
    for scope in ("principal_only", "tenant_shared"):
        for vis in ("isolated", "tenant"):
            assert scoping.can_view(
                caller_type="agent", caller_id=AGENT_A,
                owner_type="agent", owner_id=AGENT_A,
                memory_scope=scope, user_scope_visibility=vis,
            )


def test_tenant_shared_crosses_only_under_tenant_policy() -> None:
    # isolated -> shared still does NOT cross.
    assert not scoping.can_view(
        caller_type="agent", caller_id=AGENT_A, owner_type="agent", owner_id=AGENT_B,
        memory_scope="tenant_shared", user_scope_visibility="isolated",
    )
    # tenant -> shared crosses.
    assert scoping.can_view(
        caller_type="agent", caller_id=AGENT_A, owner_type="agent", owner_id=AGENT_B,
        memory_scope="tenant_shared", user_scope_visibility="tenant",
    )


# ── End-to-end through the API (the actual leak regression) ──────────────────────────
@pytest.mark.asyncio
async def test_search_cannot_leak_other_principal_principal_only(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client

    # B stores a PRIVATE memory.
    bind_principal(app, make_principal(agent_id=AGENT_B))
    rb = await ac.post(
        "/v1/memories",
        json={"content": "B's secret diary entry", "scope": "principal_only"},
    )
    assert rb.status_code == 201

    # A searches for it — MUST NOT see it (same tenant, different principal).
    bind_principal(app, make_principal(agent_id=AGENT_A))
    s = await ac.post("/v1/memories/search", json={"query": "secret diary", "top_k": 50})
    assert s.status_code == 200
    contents = [m["content"] for m in s.json()["results"]]
    assert "B's secret diary entry" not in contents
    assert s.json()["count"] == 0


@pytest.mark.asyncio
async def test_tenant_shared_invisible_under_isolated_default(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Default tenant visibility is 'isolated' -> even tenant_shared does not cross.
    bind_principal(app, make_principal(agent_id=AGENT_B))
    await ac.post("/v1/memories", json={"content": "B shared note", "scope": "tenant_shared"})

    bind_principal(app, make_principal(agent_id=AGENT_A))
    s = await ac.post("/v1/memories/search", json={"query": "shared note", "top_k": 50})
    assert "B shared note" not in [m["content"] for m in s.json()["results"]]


@pytest.mark.asyncio
async def test_tenant_shared_visible_under_tenant_policy(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.repo.set_tenant_visibility(make_principal().tenant_id, "tenant")

    bind_principal(app, make_principal(agent_id=AGENT_B))
    await ac.post("/v1/memories", json={"content": "B shared fact", "scope": "tenant_shared"})

    bind_principal(app, make_principal(agent_id=AGENT_A))
    s = await ac.post("/v1/memories/search", json={"query": "shared fact", "top_k": 50})
    assert "B shared fact" in [m["content"] for m in s.json()["results"]]


@pytest.mark.asyncio
async def test_tenant_policy_still_hides_principal_only(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Even with the most permissive 'tenant' policy, principal_only NEVER crosses.
    app.state.repo.set_tenant_visibility(make_principal().tenant_id, "tenant")
    bind_principal(app, make_principal(agent_id=AGENT_B))
    await ac.post("/v1/memories", json={"content": "B private under tenant policy",
                                        "scope": "principal_only"})

    bind_principal(app, make_principal(agent_id=AGENT_A))
    s = await ac.post("/v1/memories/search", json={"query": "private under tenant", "top_k": 50})
    assert "B private under tenant policy" not in [m["content"] for m in s.json()["results"]]


@pytest.mark.asyncio
async def test_by_id_other_principal_is_404_not_403(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(agent_id=AGENT_B))
    rb = await ac.post("/v1/memories", json={"content": "B owned", "scope": "principal_only"})
    mem_id = rb.json()["id"]

    # A tries to GET B's memory by id -> 404 (anti-existence-leak), never 403.
    bind_principal(app, make_principal(agent_id=AGENT_A))
    g = await ac.get(f"/v1/memories/{mem_id}")
    assert g.status_code == 404, g.text
    assert g.json()["error"]["code"] == "NOT_FOUND"
