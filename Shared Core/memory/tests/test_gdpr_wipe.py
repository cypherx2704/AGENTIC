"""GDPR bulk wipe — atomicity (log + delete + event in one unit) + scoping."""

from __future__ import annotations

import pytest

from _helpers import AGENT_A, AGENT_B, bind_principal, make_principal


@pytest.mark.asyncio
async def test_wipe_deletes_log_and_emits_event_atomically(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    p = make_principal(agent_id=AGENT_A)
    bind_principal(app, p)

    for i in range(3):
        await ac.post("/v1/memories", json={"content": f"a memory {i}"})
    await ac.post("/v1/sessions", json={"session_id": "s1"})

    r = await ac.post("/v1/gdpr/wipe", json={"reason": "user requested erasure"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_count"] == 3
    assert body["principal_id"] == AGENT_A
    assert body["wipe_log_id"]

    repo = app.state.repo
    # 1) memories gone
    count, _ = await repo.resource_usage(p.tenant_id, "agent", AGENT_A)
    assert count == 0
    # 2) wipe log written
    assert any(e["id"] == body["wipe_log_id"] for e in repo._wipe_log)
    # 3) gdpr.wiped outbox event emitted (same unit of work as the delete + log)
    wiped_events = [e for e in repo.events if e["topic"] == "cypherx.memory.gdpr.wiped"]
    assert len(wiped_events) == 1
    assert wiped_events[0]["deleted_count"] == 3
    assert wiped_events[0]["wipe_log_id"] == body["wipe_log_id"]


@pytest.mark.asyncio
async def test_wipe_only_targets_the_named_principal(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # B stores a memory.
    bind_principal(app, make_principal(agent_id=AGENT_B))
    await ac.post("/v1/memories", json={"content": "B keeps this"})

    # A wipes ITS OWN principal -> B's memory survives.
    bind_principal(app, make_principal(agent_id=AGENT_A))
    await ac.post("/v1/memories", json={"content": "A will be wiped"})
    r = await ac.post("/v1/gdpr/wipe", json={})
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 1

    repo = app.state.repo
    b_count, _ = await repo.resource_usage(make_principal().tenant_id, "agent", AGENT_B)
    assert b_count == 1  # B untouched


@pytest.mark.asyncio
async def test_wipe_explicit_target_requires_both_fields(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    r = await ac.post("/v1/gdpr/wipe", json={"principal_type": "user"})  # missing principal_id
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
