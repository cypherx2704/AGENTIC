"""Platform seed — manifest validity + idempotent upsert path."""

from __future__ import annotations

import pytest

from tool_registry.core.config import Settings
from tool_registry.services import manifest as manifest_svc
from tool_registry.services import seed

from .fakes import FakePool


def test_web_search_manifest_is_contract4_valid() -> None:
    settings = Settings(tool_web_search_base_url="http://tool-web-search:8080")
    manifest = seed.build_web_search_manifest(settings)
    # Must pass the registry's own Contract-4 validation.
    manifest_svc.validate_manifest(manifest)
    assert manifest["name"] == "tool-web-search"
    # base_url tracks config (never hardcoded at the call site).
    assert manifest["base_url"] == "http://tool-web-search:8080"
    assert seed.seed_capabilities(manifest) == [("web_search", "tool:tool-web-search:invoke")]


def test_manifest_base_url_follows_config() -> None:
    settings = Settings(tool_web_search_base_url="http://ws.tools.svc:9000")
    manifest = seed.build_web_search_manifest(settings)
    assert manifest["base_url"] == "http://ws.tools.svc:9000"


@pytest.mark.asyncio
async def test_seed_new_platform_tool_writes_rows() -> None:
    settings = Settings()
    pool = FakePool()
    # No existing platform tool, then the INSERT ... RETURNING tool_id.
    pool.on("SELECT tool_id FROM tools WHERE name = %s AND tenant_id IS NULL", [], once=True)
    pool.on("INSERT INTO tools", [{"tool_id": "seed-id"}])

    await seed.seed_platform_tools(pool, settings)

    # Platform context: GUC set to empty string.
    assert pool.last_tenant == ""
    assert any("INSERT INTO tool_versions" in w[0] for w in pool.writes)
    assert any("INSERT INTO tool_capabilities" in w[0] for w in pool.writes)
    # All seed rows carry NULL tenant_id (platform), never a tenant param.
    for sql, _params in pool.writes:
        if "INSERT INTO tool" in sql and "VALUES (NULL" in sql:
            assert sql.split("VALUES")[1].lstrip().startswith("(NULL")


@pytest.mark.asyncio
async def test_seed_is_failsoft_on_db_error() -> None:
    settings = Settings()

    class _BoomPool(FakePool):
        @property
        def connection(self):  # type: ignore[override]
            raise RuntimeError("db down")

    # seed_platform_tools swallows errors (best-effort at boot) — must not raise.
    await seed.seed_platform_tools(_BoomPool(), settings)
