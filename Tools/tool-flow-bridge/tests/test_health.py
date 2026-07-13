"""Health endpoint tests."""

from __future__ import annotations


async def test_livez_ok(make_client):
    client = await make_client()
    resp = await client.get("/livez")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readyz_reports_checks(make_client):
    client = await make_client()
    resp = await client.get("/readyz")
    # No reachable Postgres in tests -> not ready (503), but the body always carries checks.
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "postgres" in body["checks"]
    assert "valkey" in body["checks"]


async def test_metrics(make_client):
    client = await make_client()
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert b"tfb_" in resp.content
