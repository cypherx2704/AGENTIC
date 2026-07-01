"""Health + metrics endpoints (Contract 7)."""

from __future__ import annotations

import pytest

from .conftest import DownValkey, FakeValkey


@pytest.mark.asyncio
async def test_livez_ok(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.get("/livez")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_ready_with_valkey(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client(valkey=FakeValkey())
    resp = await ac.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["valkey"] == "ok"


@pytest.mark.asyncio
async def test_readyz_ready_even_without_valkey(make_client) -> None:  # type: ignore[no-untyped-def]
    # Valkey is a SOFT dependency: ready stays True, only the check string flips.
    ac = await make_client(valkey=DownValkey())
    resp = await ac.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True
    assert resp.json()["checks"]["valkey"] == "unavailable"


@pytest.mark.asyncio
async def test_metrics_exposition(make_client) -> None:  # type: ignore[no-untyped-def]
    ac = await make_client()
    resp = await ac.get("/metrics")
    assert resp.status_code == 200
    assert "tws_" in resp.text  # service metric prefix present
