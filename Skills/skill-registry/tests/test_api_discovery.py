"""GET /v1/skills + GET /v1/skills/{name} — discovery UNION, shadowing, version pinning.

Drives the real ASGI app with the auth dependency overridden and a FakePool scripted to
return the skill/version/capability/health rows db.queries expects. No live infra.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://skill_user:localdev@localhost:5432/cypherx_platform")
os.environ.setdefault("SEED_PLATFORM_SKILLS", "false")

from skill_registry.core.auth import Principal, require_principal  # noqa: E402
from skill_registry.main import create_app  # noqa: E402

from .fakes import FakePool  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"


def _principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["skill:invoke"], principal_type="agent")


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        app.state.valkey = None
        app.state.http_client = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


def _manifest(name: str, version: str, base: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "protocol_version": "mcp/1.0",
        "name": name,
        "version": version,
        "description": "x",
        "base_url": base,
        "required_scopes": ["skill:invoke", f"skill:{name}:invoke"],
        "skills": [{"name": "web_search", "description": "d", "input_schema": {"type": "object"}}],
    }


@pytest.mark.asyncio
async def test_list_skills_union_with_tenant_shadowing(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    # UNION: a platform skill-web-search + the tenant's OWN skill-web-search (shadows) +
    # a platform-only skill-translate.
    pool.on(
        "FROM skills t ORDER BY t.name",
        [
            {"skill_id": "plat-ws", "name": "skill-web-search", "tenant_id": None,
             "status": "active", "latest_version": "1.0.0", "is_platform": True},
            {"skill_id": "tenant-ws", "name": "skill-web-search", "tenant_id": TENANT,
             "status": "active", "latest_version": "2.0.0", "is_platform": False},
            {"skill_id": "plat-tr", "name": "skill-translate", "tenant_id": None,
             "status": "active", "latest_version": "1.0.0", "is_platform": True},
        ],
    )
    # Version + capability + health responders (shared across skills for simplicity).
    pool.on("FROM skill_versions WHERE skill_id = %s AND status = %s ORDER BY created_at",
            [{"version": "2.0.0", "manifest": _manifest("skill-web-search", "2.0.0", "http://t:8080"),
              "status": "active", "created_at": "now"}])
    pool.on("FROM skill_capabilities WHERE skill_id",
            [{"capability": "web_search", "required_scope": "skill:skill-web-search:invoke"}])
    pool.on("FROM skill_health WHERE skill_id", [{"status": "active", "last_etag": None,
            "consecutive_failures": 0, "last_polled": None}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/skills")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    by_name = {d["name"]: d for d in data}
    # Tenant priority: only ONE skill-web-search, and it's the tenant's.
    assert by_name["skill-web-search"]["owner"] == "tenant"
    assert "skill-translate" in by_name
    assert len(data) == 2
    # The RLS GUC was set to the Principal's tenant.
    assert pool.last_tenant == TENANT


@pytest.mark.asyncio
async def test_get_skill_by_name_resolves_latest_active(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM skills WHERE name = %s",
            [{"skill_id": "plat-ws", "name": "skill-web-search", "tenant_id": None,
              "status": "active", "latest_version": "1.2.0", "is_platform": True}])
    pool.on("ORDER BY created_at DESC LIMIT 1",
            [{"version": "1.2.0", "manifest": _manifest("skill-web-search", "1.2.0", "http://ws:8080"),
              "status": "active", "created_at": "now"}])
    pool.on("FROM skill_capabilities WHERE skill_id",
            [{"capability": "web_search", "required_scope": "skill:skill-web-search:invoke"}])
    pool.on("FROM skill_health WHERE skill_id", [{"status": "active"}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/skills/skill-web-search")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "skill-web-search"
    assert body["version"] == "1.2.0"
    assert body["invoke_url"] == "http://ws:8080"
    assert body["required_scopes"] == ["skill:invoke", "skill:skill-web-search:invoke"]
    assert body["owner"] == "platform"


@pytest.mark.asyncio
async def test_get_skill_version_pinning(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM skills WHERE name = %s",
            [{"skill_id": "t", "name": "skill-x", "tenant_id": TENANT,
              "status": "active", "latest_version": "3.0.0", "is_platform": False}])
    # The version-pinned query path: WHERE ... AND version = %s.
    pool.on("AND version = %s AND status = %s",
            [{"version": "1.0.0", "manifest": _manifest("skill-x", "1.0.0", "http://x:8080"),
              "status": "active", "created_at": "old"}])
    pool.on("FROM skill_capabilities WHERE skill_id", [])
    pool.on("FROM skill_health WHERE skill_id", [{"status": "active"}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/skills/skill-x", params={"version": "1.0.0"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_get_skill_unknown_name_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool().on("FROM skills WHERE name = %s", [])
    app.state.db_pool = pool
    resp = await ac.get("/v1/skills/nope")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_skill_missing_pinned_version_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM skills WHERE name = %s",
            [{"skill_id": "t", "name": "skill-x", "tenant_id": TENANT,
              "status": "active", "latest_version": "3.0.0", "is_platform": False}])
    pool.on("AND version = %s AND status = %s", [])  # no such active version
    app.state.db_pool = pool
    resp = await ac.get("/v1/skills/skill-x", params={"version": "9.9.9"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_discovery_503_when_no_db(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None
    resp = await ac.get("/v1/skills")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
