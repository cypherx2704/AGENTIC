"""Session create — idempotent for the same principal; 409 cross-principal collision."""

from __future__ import annotations

import pytest

from _helpers import AGENT_A, AGENT_B, bind_principal, make_principal


@pytest.mark.asyncio
async def test_create_session(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post("/v1/sessions", json={"session_id": "chat-1", "title": "My chat"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session_id"] == "chat-1"
    assert body["principal_type"] == "agent"
    assert body["principal_id"] == AGENT_A


@pytest.mark.asyncio
async def test_create_session_idempotent_same_principal(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    first = await ac.post("/v1/sessions", json={"session_id": "chat-1"})
    second = await ac.post("/v1/sessions", json={"session_id": "chat-1"})
    assert first.status_code == 201
    assert second.status_code == 201  # idempotent: returns the existing session, no error
    assert second.json()["session_id"] == "chat-1"


@pytest.mark.asyncio
async def test_create_session_cross_principal_collision_409(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal(agent_id=AGENT_A))
    a = await ac.post("/v1/sessions", json={"session_id": "shared-id"})
    assert a.status_code == 201

    # A DIFFERENT principal claims the same session_id -> 409 collision.
    bind_principal(app, make_principal(agent_id=AGENT_B))
    b = await ac.post("/v1/sessions", json={"session_id": "shared-id"})
    assert b.status_code == 409, b.text
    assert b.json()["error"]["code"] == "CONFLICT"
    assert b.json()["error"]["details"]["reason"] == "SESSION_PRINCIPAL_COLLISION"
