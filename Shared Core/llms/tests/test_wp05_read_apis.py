"""WP05 read surface — GET /v1/models, /v1/usage, /v1/cost (Contract-19 / 1d).

Deterministic, no live infra. Drives the ASGI app with ``mock_providers=true`` and
the standard ``require_principal`` override. The DB is faked two ways:

* ``app.state.db_pool = None`` exercises the degraded paths — /models falls back to
  the in-process capability catalog; /usage and /cost return 503 (Contract: the
  usage store is unavailable).
* A ``_FakePool`` duck-types the psycopg ``AsyncConnectionPool`` interface that
  ``db.read_queries.in_tenant`` exercises (``connection()`` async-CM ->
  ``conn.transaction()`` async-CM, ``conn.execute(...)``, and a
  ``conn.cursor(row_factory=...).execute(...).fetchall()``). It returns canned
  aggregation rows so grouping / sums / response shape are asserted without Postgres.

Tenant (Contract 13) is taken ONLY from the JWT Principal — the fake pool captures
the ``app.tenant_id`` set_config value so we can assert a body/query tenant cannot
override it.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.auth import Principal, require_principal  # noqa: E402
from llms_gateway.main import create_app  # noqa: E402

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _fake_principal() -> Principal:
    return Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["llm:invoke"],
        principal_type="agent",
    )


# ── Fake psycopg pool that returns canned aggregation rows ─────────────────────
class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        return self

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, owner: _FakePool) -> None:
        self._owner = owner

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield self

    async def execute(self, sql: str, params: Any = None) -> Any:
        # The only direct conn.execute in read_queries.in_tenant is the
        # set_config('app.tenant_id', %s, true) — capture the tenant it sets so a
        # test can prove the tenant came from the Principal, not a body/query param.
        if "set_config" in sql and params:
            self._owner.set_config_tenant = params[0]
        return _FakeCursor([])

    def cursor(self, *, row_factory: Any = None) -> _FakeCursor:
        return _FakeCursor(self._owner.rows)


class _FakePool:
    """Minimal stand-in for psycopg's AsyncConnectionPool used by read_queries."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.set_config_tenant: str | None = None

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield _FakeConn(self)


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _fake_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None  # default: degraded path; tests override per-case
        app.state.valkey = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


# ── GET /v1/models ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_models_catalog_shape_with_no_db(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None  # catalog fallback (no alias rows)

    resp = await ac.get("/v1/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "data" in body and isinstance(body["data"], list)
    assert body["data"], "expected a non-empty model catalog"

    by_id = {m["id"]: m for m in body["data"]}
    # The cold-start capability registry literals must all be present.
    for model_id in ("claude-opus-4-8", "claude-sonnet-4-6", "gpt-4o"):
        assert model_id in by_id, f"{model_id} missing from /v1/models"

    entry = by_id["claude-opus-4-8"]
    assert entry["provider"] == "anthropic"
    assert isinstance(entry["aliases"], list)
    caps = entry["capabilities"]
    assert caps["max_tokens_cap"] == 32000
    assert caps["context_window"] == 200000
    assert set(caps) >= {
        "max_tokens_cap",
        "context_window",
        "supports_vision",
        "supports_tools",
        "supports_streaming",
        "embedding_dim",
    }


@pytest.mark.asyncio
async def test_models_includes_tenant_aliases_from_pool(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(
        rows=[
            {"alias": "smart", "model_id": "claude-sonnet-4-6", "provider": "anthropic"},
            {"alias": "fast", "model_id": "claude-haiku-4-5", "provider": "anthropic"},
        ]
    )
    resp = await ac.get("/v1/models")
    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["data"]}
    assert "smart" in by_id["claude-sonnet-4-6"]["aliases"]
    assert "fast" in by_id["claude-haiku-4-5"]["aliases"]
    # The tenant whose RLS scope was used is the Principal's, not anything client-set.
    assert app.state.db_pool.set_config_tenant == TEST_TENANT


# ── GET /v1/usage ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_usage_503_when_no_db(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None
    resp = await ac.get("/v1/usage")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_usage_grouping_sums_and_shape(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(
        rows=[
            {
                "model": "claude-sonnet-4-6",
                "prompt_tokens": 1200,
                "completion_tokens": 340,
                "total_tokens": 1540,
                "request_count": 7,
            },
            {
                "model": "gpt-4o",
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "total_tokens": 60,
                "request_count": 1,
            },
        ]
    )
    resp = await ac.get("/v1/usage", params={"group_by": "model"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["group_by"] == ["model"]
    assert "from" in body and "to" in body  # window echoed back (from is clamped, to may be None)
    rows = {r["model"]: r for r in body["data"]}
    assert rows["claude-sonnet-4-6"]["total_tokens"] == 1540
    assert rows["claude-sonnet-4-6"]["request_count"] == 7
    assert rows["gpt-4o"]["prompt_tokens"] == 50
    # cost_usd is NOT part of the usage projection.
    assert "cost_usd" not in rows["gpt-4o"]
    assert app.state.db_pool.set_config_tenant == TEST_TENANT


@pytest.mark.asyncio
async def test_usage_invalid_group_by_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(rows=[])
    resp = await ac.get("/v1/usage", params={"group_by": "tenant"})  # not allowlisted
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert "tenant" in err["details"]["invalid"]
    assert set(err["details"]["allowed"]) == {"model", "agent", "api_key", "date"}


@pytest.mark.asyncio
async def test_usage_default_group_by_is_date(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(rows=[])
    resp = await ac.get("/v1/usage")
    assert resp.status_code == 200, resp.text
    assert resp.json()["group_by"] == ["date"]


@pytest.mark.asyncio
async def test_usage_tenant_param_cannot_override_principal(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(rows=[])
    # An attacker-supplied tenant query param must be ignored entirely (Contract 13).
    resp = await ac.get(
        "/v1/usage",
        params={"group_by": "model", "tenant_id": "11111111-1111-1111-1111-111111111111"},
    )
    assert resp.status_code == 200, resp.text
    # The RLS GUC was set to the Principal's tenant — never the spoofed param.
    assert app.state.db_pool.set_config_tenant == TEST_TENANT


# ── GET /v1/cost ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cost_503_when_no_db(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None
    resp = await ac.get("/v1/cost")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_cost_grouping_sums_and_decimal_serialised(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # cost_usd arrives as a Decimal-like value from psycopg; the API floats it.
    from decimal import Decimal

    app.state.db_pool = _FakePool(
        rows=[
            {
                "model": "claude-sonnet-4-6",
                "cost_usd": Decimal("1.2345"),
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "request_count": 3,
            },
        ]
    )
    resp = await ac.get("/v1/cost", params={"group_by": "model"})
    assert resp.status_code == 200, resp.text
    row = resp.json()["data"][0]
    assert row["cost_usd"] == pytest.approx(1.2345)
    assert isinstance(row["cost_usd"], float)
    assert row["request_count"] == 3
    assert app.state.db_pool.set_config_tenant == TEST_TENANT


@pytest.mark.asyncio
async def test_cost_invalid_group_by_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(rows=[])
    resp = await ac.get("/v1/cost", params={"group_by": "model,bogus"})
    assert resp.status_code == 422, resp.text
    assert "bogus" in resp.json()["error"]["details"]["invalid"]


@pytest.mark.asyncio
async def test_usage_invalid_timestamp_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(rows=[])
    resp = await ac.get("/v1/usage", params={"from": "not-a-date"})
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["param"] == "from"


@pytest.mark.asyncio
async def test_usage_from_after_to_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _FakePool(rows=[])
    resp = await ac.get(
        "/v1/usage",
        params={"from": "2026-06-10T00:00:00Z", "to": "2026-06-01T00:00:00Z"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
