"""Manifest health poll — poll_manifest classification + poll_one orchestration.

Drives the real poller against a fake HTTP client + fake pool (no network, no DB),
asserting: 200 -> active + manifest cached; 304 -> active + If-None-Match sent + etag
preserved; error/5xx -> degraded then offline after N consecutive failures; recovery
back to active.
"""

from __future__ import annotations

import pytest

from tool_registry.core.config import Settings
from tool_registry.services.health_poll import (
    STATUS_ACTIVE,
    STATUS_DEGRADED,
    STATUS_OFFLINE,
    HealthState,
    poll_manifest,
)
from tool_registry.services.health_runner import poll_one

from .fakes import FakeHttpClient, FakePool, FakeResponse

SETTINGS = Settings(health_degrade_after=1, health_offline_after=3, health_poll_timeout_seconds=1.0)
TOOL_ID = "00000000-0000-0000-0000-0000000000aa"
BASE = "http://tool-x:8080"

_MANIFEST = {"schema_version": "1.0.0", "name": "tool-x"}


# ── poll_manifest classification ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_poll_200_changed_with_etag() -> None:
    client = FakeHttpClient([FakeResponse(200, etag='"v1"', body=_MANIFEST)])
    res = await poll_manifest(client, BASE, last_etag=None, timeout=1.0)
    assert res.success and res.changed
    assert res.etag == '"v1"'
    assert res.manifest == _MANIFEST
    # The URL got the /manifest suffix; no If-None-Match on the first fetch.
    assert client.calls[0]["url"] == "http://tool-x:8080/manifest"
    assert "If-None-Match" not in client.calls[0]["headers"]


@pytest.mark.asyncio
async def test_poll_304_sends_if_none_match_and_is_success_unchanged() -> None:
    client = FakeHttpClient([FakeResponse(304)])
    res = await poll_manifest(client, BASE, last_etag='"v1"', timeout=1.0)
    assert res.success and not res.changed
    assert res.etag is None  # 304 carries no new etag
    assert client.calls[0]["headers"]["If-None-Match"] == '"v1"'


@pytest.mark.asyncio
async def test_poll_5xx_is_failure() -> None:
    client = FakeHttpClient([FakeResponse(503)])
    res = await poll_manifest(client, BASE, last_etag=None, timeout=1.0)
    assert not res.success
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_poll_timeout_is_failsoft_failure() -> None:
    client = FakeHttpClient([TimeoutError("slow")])
    res = await poll_manifest(client, BASE, last_etag=None, timeout=1.0)
    assert not res.success
    assert res.error is not None  # captured, not raised


@pytest.mark.asyncio
async def test_poll_200_with_bad_json_is_failure() -> None:
    client = FakeHttpClient([FakeResponse(200, etag='"v"', body=ValueError("boom"))])
    res = await poll_manifest(client, BASE, last_etag=None, timeout=1.0)
    assert not res.success


# ── poll_one orchestration: full state transitions + persistence ────────────────
@pytest.mark.asyncio
async def test_poll_one_transitions_active_degraded_offline_active() -> None:
    pool = FakePool()
    # Script: ok -> error -> error -> error -> 304(recover? no: 304 is success) ...
    # We exercise: success(active) -> 2x error -> 1x error(offline) -> 304(active).
    client = FakeHttpClient(
        [
            FakeResponse(200, etag='"v1"', body=_MANIFEST),  # active, cache etag v1
            FakeResponse(500),                                # fail #1 -> degraded
            FakeResponse(500),                                # fail #2 -> still degraded
            FakeResponse(500),                                # fail #3 -> offline
            FakeResponse(304),                                # success -> active (etag kept)
        ]
    )
    state = HealthState()

    state = await poll_one(pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE, current=state)
    assert state.status == STATUS_ACTIVE
    assert state.last_etag == '"v1"'

    state = await poll_one(pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE, current=state)
    assert state.status == STATUS_DEGRADED
    assert state.consecutive_failures == 1

    state = await poll_one(pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE, current=state)
    assert state.status == STATUS_DEGRADED
    assert state.consecutive_failures == 2

    state = await poll_one(pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE, current=state)
    assert state.status == STATUS_OFFLINE
    assert state.consecutive_failures == 3

    # On the 304-recovery poll the prior etag v1 must be sent and the tool returns active.
    state = await poll_one(pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE, current=state)
    assert state.status == STATUS_ACTIVE
    assert state.consecutive_failures == 0
    assert state.last_etag == '"v1"'
    recovery_call = client.calls[-1]
    assert recovery_call["headers"]["If-None-Match"] == '"v1"'

    # Each poll persisted a tool_health UPSERT (writes captured by the fake pool).
    health_writes = [w for w in pool.writes if "tool_health" in w[0]]
    assert len(health_writes) == 5


@pytest.mark.asyncio
async def test_poll_one_persists_changed_manifest_only_on_200() -> None:
    pool = FakePool()
    client = FakeHttpClient([FakeResponse(200, etag='"v1"', body=_MANIFEST), FakeResponse(304)])

    state = HealthState()
    await poll_one(pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE, current=state)
    # The 200 wrote both tool_health AND the version manifest refresh.
    assert any("tool_versions" in w[0] and "manifest" in w[0].lower() for w in pool.writes)

    pool.writes.clear()
    await poll_one(
        pool, client, SETTINGS, tool_id=TOOL_ID, base_url=BASE,
        current=HealthState(last_etag='"v1"'),
    )
    # The 304 wrote tool_health but NOT a manifest refresh (manifest unchanged).
    assert not any("tool_versions" in w[0] for w in pool.writes)
