"""livez / readyz / metrics — Postgres is the hard gate, Valkey is soft."""

from __future__ import annotations

import contextlib
import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://tool_user:localdev@localhost:5432/cypherx_platform")
os.environ.setdefault("SEED_PLATFORM_TOOLS", "false")

from tool_registry.db.valkey import ValkeyClient  # noqa: E402
from tool_registry.main import create_app  # noqa: E402

_DEAD_VALKEY_URL = "redis://127.0.0.1:1/0"


class _FakeUpValkey:
    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class _FakeOkConn:
    async def execute(self, sql: str, params: tuple | None = None) -> object:
        return self


class _FakeOkPool:
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
async def test_livez_ok(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, ac = app_client
    resp = await ac.get("/livez")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_lifespan_wires_lazy_valkey_and_http_client(app_client) -> None:  # type: ignore[no-untyped-def]
    app, _ac = app_client
    assert isinstance(app.state.valkey, ValkeyClient)
    assert app.state.valkey._client is None  # lazy
    assert app.state.http_client is not None  # shared poll client wired
    assert app.state.health_task is not None  # poll loop started


@pytest.mark.asyncio
async def test_readyz_db_up_valkey_ok(app_client) -> None:  # type: ignore[no-untyped-def]
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
async def test_readyz_valkey_down_does_not_fail_readiness(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakeOkPool()
    app.state.valkey = ValkeyClient(_DEAD_VALKEY_URL, ping_timeout=0.5)
    resp = await ac.get("/readyz")
    assert resp.status_code == 200  # Valkey is SOFT
    assert resp.json()["checks"]["valkey"] == "unavailable"


@pytest.mark.asyncio
async def test_readyz_db_down_503(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None
    app.state.valkey = _FakeUpValkey()
    resp = await ac.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False


@pytest.mark.asyncio
async def test_metrics_exposed(app_client) -> None:  # type: ignore[no-untyped-def]
    _app, ac = app_client
    resp = await ac.get("/metrics")
    assert resp.status_code == 200
    assert b"tool_registry_" in resp.content
