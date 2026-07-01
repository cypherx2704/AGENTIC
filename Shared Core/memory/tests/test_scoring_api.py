"""End-to-end scoring / validity wiring through the API (flags default to today's behavior).

These run through the ASGI app to prove the flags + new request/response fields are wired,
that the default path is unchanged, and that the anti-leak rule survives the new fields.
"""

from __future__ import annotations

import pytest

from _helpers import AGENT_A, AGENT_B, bind_principal, make_principal


@pytest.mark.asyncio
async def test_store_reports_importance_and_defaults(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post("/v1/memories", json={"content": "remember this important fact",
                                            "type": "fact"})
    assert r.status_code == 201
    body = r.json()
    # New additive fields are present with inert defaults (no composite when scoring off).
    assert 0.0 <= body["importance_score"] <= 1.0
    assert body["valid_until"] is None
    assert body["superseded_by_id"] is None
    assert "composite_score" not in body or body["composite_score"] is None


@pytest.mark.asyncio
async def test_caller_supplied_importance_honoured(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post("/v1/memories", json={"content": "x", "importance": 0.42})
    assert r.status_code == 201
    assert r.json()["importance_score"] == 0.42


@pytest.mark.asyncio
async def test_search_default_has_no_composite(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "the capital of france is paris"})
    s = await ac.post("/v1/memories/search", json={"query": "capital france"})
    assert s.status_code == 200
    res = s.json()["results"]
    assert res
    # scoring OFF (default) -> similarity present, composite absent.
    assert "similarity" in res[0]
    assert res[0].get("composite_score") is None


@pytest.mark.asyncio
async def test_search_with_scoring_enabled_surfaces_composite(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_scoring_enabled = True
    await ac.post("/v1/memories", json={"content": "the capital of france is paris"})
    s = await ac.post("/v1/memories/search", json={"query": "capital france"})
    assert s.status_code == 200
    res = s.json()["results"]
    assert res
    assert res[0].get("composite_score") is not None
    assert 0.0 <= res[0]["composite_score"] <= 1.0


@pytest.mark.asyncio
async def test_session_scope_filter_narrows_results(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "scoped to s1", "session_scope_id": "s1"})
    await ac.post("/v1/memories", json={"content": "scoped to s2", "session_scope_id": "s2"})
    s = await ac.post("/v1/memories/search",
                      json={"query": "scoped", "top_k": 10, "session_scope_id": "s1"})
    contents = [m["content"] for m in s.json()["results"]]
    assert "scoped to s1" in contents
    assert "scoped to s2" not in contents


@pytest.mark.asyncio
async def test_new_scope_fields_do_not_break_anti_leak(app_client) -> None:  # type: ignore[no-untyped-def]
    # The richer scope fields must NOT let A see B's principal_only memory.
    app, ac = app_client
    bind_principal(app, make_principal(agent_id=AGENT_B))
    await ac.post("/v1/memories", json={"content": "B private scoped", "scope": "principal_only",
                                        "session_scope_id": "shared-session",
                                        "agent_scope_id": "shared-agent"})
    bind_principal(app, make_principal(agent_id=AGENT_A))
    s = await ac.post("/v1/memories/search",
                      json={"query": "private scoped", "top_k": 50,
                            "session_scope_id": "shared-session"})
    assert "B private scoped" not in [m["content"] for m in s.json()["results"]]
    assert s.json()["count"] == 0


@pytest.mark.asyncio
async def test_include_superseded_override(app_client) -> None:  # type: ignore[no-untyped-def]
    # With contradiction wired on the repo, include_superseded=True returns hidden rows.
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.repo.contradiction_enabled = True
    app.state.repo.contradiction_sim_min = -1.0  # force the nearest neighbour to conflict
    # Two non-identical, lexically-overlapping, asserting memories for the same principal.
    await ac.post("/v1/memories", json={"content": "user lives in paris france"})
    await ac.post("/v1/memories", json={"content": "user lives in berlin germany now"})
    # Default current-only: superseded paris hidden.
    s1 = await ac.post("/v1/memories/search", json={"query": "user lives", "top_k": 10})
    c1 = [m["content"] for m in s1.json()["results"]]
    assert "user lives in paris france" not in c1
    # include_superseded -> both returned.
    s2 = await ac.post("/v1/memories/search",
                       json={"query": "user lives", "top_k": 10, "include_superseded": True})
    c2 = [m["content"] for m in s2.json()["results"]]
    assert "user lives in paris france" in c2
