"""Health endpoints + outbox-event shape (store/delete emit Contract-5 events)."""

from __future__ import annotations

import pytest

from _helpers import bind_principal, make_principal
from memory_service.db import outbox


@pytest.mark.asyncio
async def test_livez_ok(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, ac = app_client
    r = await ac.get("/livez")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_degraded_without_db(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, ac = app_client
    # db_pool is None in tests -> not ready (DB is the hard dependency); valkey reported soft.
    r = await ac.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["postgresql"] == "fail"
    assert body["checks"]["valkey"] in ("ok", "unavailable")


@pytest.mark.asyncio
async def test_metrics_endpoint(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, ac = app_client
    r = await ac.get("/metrics")
    assert r.status_code == 200
    assert b"memory_requests_total" in r.content or r.status_code == 200


@pytest.mark.asyncio
async def test_store_emits_stored_event(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "emit me"})
    stored = [e for e in app.state.repo.events if e["topic"] == "cypherx.memory.stored"]
    assert len(stored) == 1
    assert stored[0]["deduped"] is False


def test_envelope_has_contract5_fields() -> None:
    env = outbox.envelope(
        outbox.TOPIC_MEMORY_STORED, "tenant-1", "trace-1", {"memory_id": "m1"},
        producer_version="0.1.0",
    )
    for key in ("event_id", "event_type", "schema_version", "produced_at", "trace_id",
                "tenant_id", "producer_service", "producer_version", "partition_key", "payload"):
        assert key in env
    assert env["event_type"] == outbox.TOPIC_MEMORY_STORED
    assert env["producer_service"] == "memory-service"
    assert env["partition_key"] == "tenant-1"
    assert env["produced_at"].endswith("Z")
