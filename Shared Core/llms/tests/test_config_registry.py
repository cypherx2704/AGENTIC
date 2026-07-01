"""WP02 DB-authoritative config — capability registry, refresh loop, seed drift.

* Seed-drift guard: the in-code cold-start fallback maps (``_PLATFORM_ALIASES``,
  ``_FALLBACK_PRICING``, ``_LITERAL_PROVIDER``, ``_FALLBACK_CAPABILITIES``) are
  parsed-and-compared against the seed SQL in ``db/migrations/`` so the two can
  never drift (Amendment Log 2026-06: in-code maps are fallbacks ONLY).
* ``CapabilityRegistry``: fallback serves cold-start lookups; a DB load overrides.
* ``_reload_registries`` drives the ``llms_config_source{source=db|fallback}``
  gauge and the lifespan owns a periodic refresh task.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from prometheus_client import REGISTRY

# Force mock providers + a harmless DB URL before importing the app.
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.config import get_settings  # noqa: E402
from llms_gateway.main import _reload_registries, create_app  # noqa: E402
from llms_gateway.services.capabilities import (  # noqa: E402
    _FALLBACK_CAPABILITIES,
    CapabilityRegistry,
    ModelCapability,
)
from llms_gateway.services.cost import _FALLBACK_PRICING, PricingRow  # noqa: E402
from llms_gateway.services.router import (  # noqa: E402
    _LITERAL_PROVIDER,
    _PLATFORM_ALIASES,
    ModelRouter,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"


# ── seed SQL parsing helpers ──────────────────────────────────────────────────────────
def _migrations_sql() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("2*.sql"))
    )


def _insert_statements(sql: str, table: str) -> str:
    blocks = re.findall(rf"INSERT INTO llms\.{table}\b[^;]*;", sql)
    assert blocks, f"no INSERT INTO llms.{table} found in the seed migrations"
    return "\n".join(blocks)


def _seed_platform_aliases() -> dict[str, tuple[str, str]]:
    block = _insert_statements(_migrations_sql(), "model_aliases")
    rows = re.findall(r"\(NULL,\s*'([\w-]+)',\s*'([\w.-]+)',\s*'(\w+)'\)", block)
    return {alias: (provider, model_id) for alias, model_id, provider in rows}


def _seed_pricing() -> dict[tuple[str, str], PricingRow]:
    block = _insert_statements(_migrations_sql(), "provider_pricing")
    rows = re.findall(
        r"\('(\w+)',\s*'([\w.-]+)',\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+),",
        block,
    )
    return {
        (provider, model): PricingRow(float(ip), float(op), float(cip), float(ccp))
        for provider, model, ip, op, cip, ccp in rows
    }


def _seed_capabilities() -> dict[str, ModelCapability]:
    block = _insert_statements(_migrations_sql(), "model_capabilities")
    # The native_tool_use column (9th) is OPTIONAL in the regex so the legacy 8-column
    # seed rows (frontier + embed/rerank/classify) still parse — those default to
    # native_tool_use=True, matching the column DEFAULT and the dataclass default.
    rows = re.findall(
        r"\('([\w.-]+)',\s*'(\w+)',\s*(\d+),\s*(\d+),\s*"
        r"(true|false),\s*(true|false),\s*(true|false),\s*(NULL|\d+)"
        r"(?:,\s*(true|false))?\)",
        block,
    )
    return {
        model_id: ModelCapability(
            model_id=model_id,
            provider=provider,
            max_tokens_cap=int(cap),
            context_window=int(ctx),
            supports_vision=vision == "true",
            supports_tools=tools == "true",
            supports_streaming=streaming == "true",
            embedding_dim=None if dim == "NULL" else int(dim),
            native_tool_use=native != "false",  # '' (absent) or 'true' => True
        )
        for model_id, provider, cap, ctx, vision, tools, streaming, dim, native in rows
    }


# ── seed-drift guards: fallback maps MUST equal the seed migrations ──────────────────
def test_fallback_platform_aliases_equal_seed_sql() -> None:
    in_code = {a: (r.provider, r.model_id) for a, r in _PLATFORM_ALIASES.items()}
    assert in_code == _seed_platform_aliases()


def test_fallback_pricing_equals_seed_sql() -> None:
    assert _seed_pricing() == _FALLBACK_PRICING


def test_fallback_capabilities_equal_seed_sql() -> None:
    assert _seed_capabilities() == _FALLBACK_CAPABILITIES


def test_literal_provider_map_equals_capability_seed_sql() -> None:
    assert {m: c.provider for m, c in _seed_capabilities().items()} == _LITERAL_PROVIDER


# ── CapabilityRegistry behaviour ──────────────────────────────────────────────────────
class _DispatchCursor:
    def __init__(self, rows_by_table: dict[str, list]) -> None:
        self._rows_by_table = rows_by_table
        self._rows: list = []

    async def execute(self, sql: str, params: tuple | None = None) -> _DispatchCursor:
        for fragment, rows in self._rows_by_table.items():
            if fragment in sql:
                self._rows = rows
                break
        return self

    async def fetchall(self) -> list:
        return self._rows


class _FakeConn:
    def __init__(self, rows_by_table: dict[str, list]) -> None:
        self._rows_by_table = rows_by_table

    def cursor(self, row_factory: object | None = None) -> _DispatchCursor:
        return _DispatchCursor(self._rows_by_table)


class _FakePool:
    def __init__(self, rows_by_table: dict[str, list]) -> None:
        self._conn = _FakeConn(rows_by_table)

    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        yield self._conn


class _DownPool:
    @contextlib.asynccontextmanager
    async def connection(self, **kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")
        yield  # pragma: no cover


def test_capability_registry_serves_cold_start_fallback() -> None:
    registry = CapabilityRegistry()
    assert registry.loaded_from_db is False
    cap = registry.get("gpt-4o")
    assert cap is not None
    assert cap.provider == "openai"
    assert cap.max_tokens_cap == 16384
    assert registry.provider_for("claude-opus-4-8") == "anthropic"
    assert registry.get("no-such-model") is None
    assert registry.provider_for("no-such-model") is None


@pytest.mark.asyncio
async def test_capability_registry_db_load_overrides_fallback() -> None:
    registry = CapabilityRegistry()
    pool = _FakePool(
        {
            "model_capabilities": [
                ("gpt-4o", "openai", 9999, 128000, True, True, False, None, True),
                ("text-embedding-3-small", "openai", 1, 8191, False, False, False, 1536, True),
            ]
        }
    )
    assert await registry.load_from_db(pool) is True  # type: ignore[arg-type]
    assert registry.loaded_from_db is True
    assert registry.get("gpt-4o").max_tokens_cap == 9999  # DB row wins
    assert registry.get("gpt-4o").supports_streaming is False
    assert registry.get("text-embedding-3-small").embedding_dim == 1536
    # Models absent from the DB load keep their fallback entry (cache is merged).
    assert registry.provider_for("claude-sonnet-4-6") == "anthropic"


@pytest.mark.asyncio
async def test_capability_registry_load_failure_keeps_cache_and_returns_false() -> None:
    registry = CapabilityRegistry()
    assert await registry.load_from_db(_DownPool()) is False  # type: ignore[arg-type]
    assert registry.loaded_from_db is False
    assert registry.get("gpt-4o").max_tokens_cap == 16384  # fallback untouched


# ── config_source gauge + refresh loop wiring ────────────────────────────────────────
def _gauge(source: str) -> float | None:
    return REGISTRY.get_sample_value("llms_config_source", {"source": source})


@pytest.mark.asyncio
async def test_reload_registries_reports_fallback_when_db_down() -> None:
    settings = get_settings()
    router = ModelRouter(settings, pool=_DownPool())  # type: ignore[arg-type]
    assert await _reload_registries(_DownPool(), router) is False
    assert _gauge("fallback") == 1.0
    assert _gauge("db") == 0.0


@pytest.mark.asyncio
async def test_reload_registries_reports_db_when_all_loads_succeed() -> None:
    # Seed-identical rows so the process-wide cost/capability singletons keep the
    # exact same values other tests rely on.
    pool = _FakePool(
        {
            "provider_pricing": [
                ("anthropic", "claude-haiku-4-5", 0.0008, 0.004, 0.00008, 0.001),
            ],
            "model_aliases": [
                (None, "fast", "claude-haiku-4-5", "anthropic"),
            ],
            "model_capabilities": [
                ("claude-haiku-4-5", "anthropic", 8192, 200000, True, True, True, None, True),
            ],
        }
    )
    settings = get_settings()
    router = ModelRouter(settings, pool=pool)  # type: ignore[arg-type]
    assert await _reload_registries(pool, router) is True
    assert _gauge("db") == 1.0
    assert _gauge("fallback") == 0.0


@pytest.mark.asyncio
async def test_lifespan_owns_periodic_config_refresh_task() -> None:
    app = create_app()
    async with LifespanManager(app, startup_timeout=15):
        task = app.state.config_refresh_task
        assert task is not None
        assert not task.done()
        assert app.state.settings.config_refresh_interval_seconds > 0
    assert task.cancelled() or task.done()  # lifespan shutdown stops the loop
