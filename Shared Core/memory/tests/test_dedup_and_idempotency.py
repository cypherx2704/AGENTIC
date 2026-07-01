"""Dedup bump semantics + idempotent store without double-embed."""

from __future__ import annotations

import pytest

from _helpers import bind_principal, make_principal


@pytest.mark.asyncio
async def test_dedup_identical_content_bumps_not_inserts(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())

    first = await ac.post("/v1/memories", json={"content": "remember the milk"})
    assert first.status_code == 201
    first_id = first.json()["id"]
    assert first.json()["deduped"] is False
    assert first.json()["score"] == 1.0

    # Identical content -> deterministic identical vector -> cosine 1.0 >= 0.95 threshold.
    second = await ac.post("/v1/memories", json={"content": "remember the milk"})
    assert second.status_code == 201
    assert second.json()["id"] == first_id        # SAME row (bumped, not a new insert)
    assert second.json()["deduped"] is True
    assert second.json()["score"] == 2.0          # score bumped

    # Only ONE memory exists for the principal.
    count, _bytes = await app.state.repo.resource_usage(
        make_principal().tenant_id, "agent", make_principal().agent_id
    )
    assert count == 1


@pytest.mark.asyncio
async def test_dedup_distinct_content_inserts_new(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    a = await ac.post("/v1/memories", json={"content": "the sky is blue"})
    b = await ac.post("/v1/memories", json={"content": "quarterly revenue grew 12 percent"})
    assert a.json()["id"] != b.json()["id"]
    assert b.json()["deduped"] is False


@pytest.mark.asyncio
async def test_dedup_threshold_honoured_per_tenant(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    p = make_principal()
    bind_principal(app, p)
    # Drop the threshold to -1.0 (the cosine floor) so ANY neighbour is a "duplicate" ->
    # the second distinct store bumps the first instead of inserting.
    app.state.repo.set_tenant_dedup_threshold(p.tenant_id, -1.0)
    a = await ac.post("/v1/memories", json={"content": "alpha"})
    b = await ac.post("/v1/memories", json={"content": "totally different beta"})
    assert b.json()["deduped"] is True
    assert b.json()["id"] == a.json()["id"]


@pytest.mark.asyncio
async def test_idempotent_store_replays_without_double_embed(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    spy = app.state.embedder  # SpyEmbeddingClient
    headers = {"Idempotency-Key": "store-1"}
    payload = {"content": "idempotent please"}

    first = await ac.post("/v1/memories", headers=headers, json=payload)
    assert first.status_code == 201
    assert first.headers.get("Idempotency-Replayed") is None
    assert spy.embed_calls == 1  # embedded once

    second = await ac.post("/v1/memories", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json() == first.json()  # byte-for-byte replay
    # THE assertion: the replay did NOT embed again (short-circuit before embedding).
    assert spy.embed_calls == 1


@pytest.mark.asyncio
async def test_idempotency_in_flight_409(app_client) -> None:  # type: ignore[no-untyped-def]
    import json

    app, ac = app_client
    p = make_principal()
    bind_principal(app, p)
    # Pre-seed an in_flight marker for the principal+key.
    key = f"cypherx:mem:idem:{p.tenant_id}:agent:{p.agent_id}:busy"
    app.state.valkey.store[key] = json.dumps({"state": "in_flight"})

    r = await ac.post("/v1/memories", headers={"Idempotency-Key": "busy"}, json={"content": "x"})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "IDEMPOTENCY_REQUEST_IN_FLIGHT"


@pytest.mark.asyncio
async def test_idempotency_no_valkey_fail_open(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.valkey = None  # fail-open: no replay, both proceed
    h = {"Idempotency-Key": "no-valkey"}
    first = await ac.post("/v1/memories", headers=h, json={"content": "fail open"})
    second = await ac.post("/v1/memories", headers=h, json={"content": "fail open"})
    assert first.status_code == 201 and second.status_code == 201
    assert second.headers.get("Idempotency-Replayed") is None
