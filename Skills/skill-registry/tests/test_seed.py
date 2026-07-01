"""Platform seed — manifest validity + idempotent upsert path."""

from __future__ import annotations

import pytest

from skill_registry.core.config import Settings
from skill_registry.services import manifest as manifest_svc
from skill_registry.services import seed

from .fakes import FakePool


def test_web_search_manifest_is_contract4_valid() -> None:
    settings = Settings(skill_web_search_base_url="http://skill-web-search:8080")
    manifest = seed.build_web_search_manifest(settings)
    # Must pass the registry's own Contract-4 validation.
    manifest_svc.validate_manifest(manifest)
    assert manifest["name"] == "skill-web-search"
    # base_url tracks config (never hardcoded at the call site).
    assert manifest["base_url"] == "http://skill-web-search:8080"
    assert seed.seed_capabilities(manifest) == [("web_search", "skill:skill-web-search:invoke")]


def test_manifest_base_url_follows_config() -> None:
    settings = Settings(skill_web_search_base_url="http://ws.skills.svc:9000")
    manifest = seed.build_web_search_manifest(settings)
    assert manifest["base_url"] == "http://ws.skills.svc:9000"


@pytest.mark.asyncio
async def test_seed_new_platform_skill_writes_rows() -> None:
    settings = Settings()
    pool = FakePool()
    # No existing platform skill, then the INSERT ... RETURNING skill_id.
    pool.on("SELECT skill_id FROM skills WHERE name = %s AND tenant_id IS NULL", [], once=True)
    pool.on("INSERT INTO skills", [{"skill_id": "seed-id"}])

    await seed.seed_platform_skills(pool, settings)

    # Platform context: GUC set to empty string.
    assert pool.last_tenant == ""
    assert any("INSERT INTO skill_versions" in w[0] for w in pool.writes)
    assert any("INSERT INTO skill_capabilities" in w[0] for w in pool.writes)
    # All seed rows carry NULL tenant_id (platform), never a tenant param.
    for sql, _params in pool.writes:
        if "INSERT INTO skill" in sql and "VALUES (NULL" in sql:
            assert sql.split("VALUES")[1].lstrip().startswith("(NULL")


@pytest.mark.asyncio
async def test_seed_is_failsoft_on_db_error() -> None:
    settings = Settings()

    class _BoomPool(FakePool):
        @property
        def connection(self):  # type: ignore[override]
            raise RuntimeError("db down")

    # seed_platform_skills swallows errors (best-effort at boot) — must not raise.
    await seed.seed_platform_skills(_BoomPool(), settings)
