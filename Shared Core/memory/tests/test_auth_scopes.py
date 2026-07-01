"""Scope enforcement — read-only token cannot write; write-only cannot read."""

from __future__ import annotations

import pytest

from _helpers import bind_principal, make_principal


@pytest.mark.asyncio
async def test_read_only_token_cannot_store(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(scopes=["mem:read"]))
    r = await ac.post("/v1/memories", json={"content": "no write scope"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_write_only_token_cannot_search(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(scopes=["mem:write"]))
    r = await ac.post("/v1/memories/search", json={"query": "anything"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_write_scope_can_store(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(scopes=["mem:write"]))
    r = await ac.post("/v1/memories", json={"content": "has write scope"})
    assert r.status_code == 201


def test_memory_principal_prefers_user_over_agent() -> None:
    # An on-behalf-of-user token owns memories AS THE USER, not the agent.
    p = make_principal(agent_id="agent-x", user_id="user-y")
    assert p.memory_principal == ("user", "user-y")
    # A bare agent token owns AS THE AGENT.
    p2 = make_principal(agent_id="agent-x", user_id=None)
    assert p2.memory_principal == ("agent", "agent-x")
