"""Contract-19.1 cypherx.memory.usage.recorded — payload builder + outbox emission.

Covers the builder against the contract's required shape (tenant_id + operation + non-empty
numeric units, identity from the principal only) and the end-to-end emission on
store/search/delete through the API (captured in the in-memory repo's events list since the
test harness has no DB pool).
"""

from __future__ import annotations

import pytest

from _helpers import bind_principal, make_principal
from memory_service.db import outbox
from memory_service.services import usage


# ── Builder (contract shape) ───────────────────────────────────────────────────────────
def test_build_usage_payload_required_fields() -> None:
    p = make_principal(agent_id="agent-x")
    payload = usage.build_usage_payload(
        principal=p, operation="write", units={"items_written": 3, "embedding_tokens": 512},
        trace_id="trace-1", duration_ms=47,
    )
    # Contract: tenant_id + operation + units (>=1 numeric entry) required.
    assert payload["tenant_id"] == p.tenant_id
    assert payload["operation"] == "write"
    assert payload["units"] == {"items_written": 3.0, "embedding_tokens": 512.0}
    assert all(isinstance(v, float) for v in payload["units"].values())
    assert payload["agent_id"] == "agent-x"
    assert payload["trace_id"] == "trace-1"
    assert payload["duration_ms"] == 47


def test_build_usage_payload_identity_from_principal_only() -> None:
    # A user-on-behalf principal -> principal_id is the user; agent_id may also be present.
    p = make_principal(agent_id="agent-y", user_id="user-z")
    payload = usage.build_usage_payload(
        principal=p, operation="recall", units={"items_recalled": 5},
    )
    assert payload["principal_id"] == "user-z"
    assert payload["agent_id"] == "agent-y"


def test_build_usage_payload_never_empty_units() -> None:
    p = make_principal()
    payload = usage.build_usage_payload(principal=p, operation="delete", units={})
    assert payload["units"]  # contract requires >=1 entry
    assert all(isinstance(v, float) for v in payload["units"].values())


def test_usage_topic_constant() -> None:
    assert outbox.TOPIC_MEMORY_USAGE_RECORDED == "cypherx.memory.usage.recorded"


# ── End-to-end emission through the API ─────────────────────────────────────────────────
def _usage_events(app):  # type: ignore[no-untyped-def]
    return [e for e in app.state.repo.events if e["topic"] == "cypherx.memory.usage.recorded"]


@pytest.mark.asyncio
async def test_store_emits_usage_event(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post("/v1/memories", json={"content": "meter me"})
    assert r.status_code == 201
    events = _usage_events(app)
    assert len(events) == 1
    assert events[0]["operation"] == "write"
    assert events[0]["units"]["items_written"] == 1.0


@pytest.mark.asyncio
async def test_search_emits_recall_usage_event(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "findable note"})
    app.state.repo.events.clear()
    s = await ac.post("/v1/memories/search", json={"query": "findable", "top_k": 5})
    assert s.status_code == 200
    recalls = [e for e in _usage_events(app) if e["operation"] == "recall"]
    assert len(recalls) == 1
    assert recalls[0]["units"]["items_recalled"] >= 0.0
    # scoring OFF by default -> no 'score' op emitted.
    assert not [e for e in _usage_events(app) if e["operation"] == "score"]


@pytest.mark.asyncio
async def test_delete_emits_usage_event(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post("/v1/memories", json={"content": "delete me"})
    mid = r.json()["id"]
    app.state.repo.events.clear()
    d = await ac.delete(f"/v1/memories/{mid}")
    assert d.status_code == 200
    deletes = [e for e in _usage_events(app) if e["operation"] == "delete"]
    assert len(deletes) == 1
    assert deletes[0]["units"]["items_deleted"] == 1.0


@pytest.mark.asyncio
async def test_usage_events_disabled_flag(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_usage_events_enabled = False
    await ac.post("/v1/memories", json={"content": "no meter"})
    assert _usage_events(app) == []
