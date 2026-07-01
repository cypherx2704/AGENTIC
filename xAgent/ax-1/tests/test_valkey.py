"""WP02 — Valkey wiring foundation (SOFT dependency).

``ValkeyClient`` is the lazy async client over ``Settings.valkey_url`` (the same shape
the llms-gateway uses). Pinned behaviours:

  * LAZY: constructing the wrapper opens no connection; the client exists only after
    first use, so a Valkey outage can never fail boot.
  * ``ping()`` never raises: failure -> False + ``xagent_valkey_up`` gauge 0;
    success -> True + gauge 1.
  * ``/readyz`` SOFT-reports Valkey in the checks map but NEVER gates readiness on it
    (readiness = Postgres + Auth JWKS only, per the Phase 9 K8s spec).
"""

from __future__ import annotations

from typing import Any

from prometheus_client import REGISTRY

from agent_runtime.api import health as health_mod
from agent_runtime.services.valkey import ValkeyClient


def _valkey_up_value() -> float | None:
    return REGISTRY.get_sample_value("xagent_valkey_up")


# ── lazy construction ───────────────────────────────────────────────────────────────
def test_client_is_lazy_until_first_use() -> None:
    vc = ValkeyClient("redis://localhost:6379/0")
    assert vc._client is None  # no connection attempt at construction time


# ── ping failure -> False + gauge 0 (never raises) ──────────────────────────────────
async def test_ping_failure_returns_false_and_gauges_zero() -> None:
    # Port 1 is never a Valkey; the 2s socket timeouts keep the failure fast.
    vc = ValkeyClient("redis://localhost:1/0")
    try:
        assert await vc.ping() is False
        assert _valkey_up_value() == 0.0
    finally:
        await vc.aclose()


# ── ping success -> True + gauge 1 ──────────────────────────────────────────────────
async def test_ping_success_returns_true_and_gauges_one() -> None:
    class _FakeRedis:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    vc = ValkeyClient("redis://localhost:6379/0")
    vc._client = _FakeRedis()  # type: ignore[assignment] — inject the fake (no network)
    try:
        assert await vc.ping() is True
        assert _valkey_up_value() == 1.0
    finally:
        await vc.aclose()


# ── /readyz: soft-report only — Valkey failure never flips readiness ─────────────────
async def test_readyz_reports_valkey_but_never_gates_on_it(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app

    async def _db_ok(pool: Any) -> bool:
        return True

    monkeypatch.setattr(health_mod, "readyz_ping", _db_ok)
    monkeypatch.setattr(health_mod, "_jwks_ready", lambda _url: True)
    app.state.db_pool = object()  # readyz_ping is patched; the pool is a handle only

    class _DownValkey:
        async def ping(self) -> bool:
            return False

        async def aclose(self) -> None:  # lifespan shutdown calls this — match the client API
            return None

    app.state.valkey = _DownValkey()

    resp = await client.get("/readyz")
    assert resp.status_code == 200, resp.text  # READY despite Valkey being down
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["valkey"] == "fail"  # ... but honestly reported
    assert body["checks"]["postgresql"] == "ok"
    assert body["checks"]["auth_jwks"] == "ok"


async def test_readyz_includes_valkey_check_from_lifespan_wiring(client) -> None:  # type: ignore[no-untyped-def]
    # The lifespan wires a ValkeyClient onto app.state (the fixture swaps it for a
    # network-free double); /readyz reports the valkey check either way (ok/fail)
    # WITHOUT gating. db_pool is nulled by the fixture -> 503 from the HARD Postgres
    # gate, never from Valkey.
    resp = await client.get("/readyz")
    body = resp.json()
    assert "valkey" in body["checks"]
    assert body["checks"]["postgresql"] == "fail"
    assert resp.status_code == 503  # hard gate: Postgres (and/or JWKS), not Valkey
