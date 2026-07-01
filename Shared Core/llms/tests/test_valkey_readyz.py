"""WP02 Valkey foundation — soft dependency wiring only (features land in WP05).

* ``/readyz`` reports ``valkey: "ok" | "unavailable"`` WITHOUT gating readiness on
  it: DB up + Valkey down -> 200; DB down + Valkey up -> 503.
* The ``llms_valkey_up`` gauge tracks ping outcomes.
* The lifespan wires a lazy ``ValkeyClient`` (no TCP connect at boot).
"""

from __future__ import annotations

import contextlib
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY

# Force mock providers + a harmless DB URL before importing the app.
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.db.valkey import ValkeyClient  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402

# An unroutable-in-practice local port so pings fail fast and deterministically.
_DEAD_VALKEY_URL = "redis://127.0.0.1:1/0"


class _FakeUpValkey:
    """Duck-typed stand-in for ValkeyClient with Valkey reachable."""

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _FakeOkConn:
    async def execute(self, sql: str, params: tuple | None = None) -> object:
        return self


class _FakeOkPool:
    """Minimal pool whose readyz ping always succeeds (Postgres 'up')."""

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield _FakeOkConn()


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    async with LifespanManager(app, startup_timeout=15):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


@pytest.mark.asyncio
async def test_lifespan_wires_lazy_valkey_client(app_client) -> None:  # type: ignore[no-untyped-def]
    app, _ac = app_client
    valkey = app.state.valkey
    assert isinstance(valkey, ValkeyClient)
    # Lazy: no underlying redis client is created until first use.
    assert valkey._client is None


@pytest.mark.asyncio
async def test_readyz_reports_valkey_ok_without_gating(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakeOkPool()
    app.state.valkey = _FakeUpValkey()

    resp = await ac.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["postgresql"] == "ok"
    assert body["checks"]["valkey"] == "ok"


@pytest.mark.asyncio
async def test_readyz_valkey_unavailable_does_not_fail_readiness(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakeOkPool()
    # Real client pointed at a dead port — exercises the actual fail-soft ping path.
    app.state.valkey = ValkeyClient(_DEAD_VALKEY_URL, ping_timeout=0.5)

    resp = await ac.get("/readyz")
    assert resp.status_code == 200  # Valkey is SOFT: readiness still passes
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["valkey"] == "unavailable"
    assert REGISTRY.get_sample_value("llms_valkey_up") == 0.0


@pytest.mark.asyncio
async def test_readyz_db_down_still_reports_valkey_state(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None  # Postgres is the HARD dependency
    app.state.valkey = _FakeUpValkey()

    resp = await ac.get("/readyz")
    assert resp.status_code == 503  # valkey 'ok' cannot rescue readiness either
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["postgresql"] == "fail"
    assert body["checks"]["valkey"] == "ok"


@pytest.mark.asyncio
async def test_valkey_up_gauge_set_on_successful_ping() -> None:
    from llms_gateway.core.config import get_settings

    client = ValkeyClient(get_settings().valkey_url, ping_timeout=1.0)
    try:
        if not await client.ping():
            pytest.skip("local Valkey unavailable")
        assert REGISTRY.get_sample_value("llms_valkey_up") == 1.0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_valkey_client_close_is_safe_when_never_connected() -> None:
    client = ValkeyClient(_DEAD_VALKEY_URL, ping_timeout=0.5)
    await client.close()  # no-op, must not raise
