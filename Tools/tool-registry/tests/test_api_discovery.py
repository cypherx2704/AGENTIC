"""GET /v1/tools + GET /v1/tools/{name} — discovery UNION, shadowing, version pinning.

Drives the real ASGI app with the auth dependency overridden and a FakePool scripted to
return the tool/version/capability/health rows db.queries expects. No live infra.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://tool_user:localdev@localhost:5432/cypherx_platform")
os.environ.setdefault("SEED_PLATFORM_TOOLS", "false")

from tool_registry.core.auth import Principal, require_principal  # noqa: E402
from tool_registry.main import create_app  # noqa: E402

from .fakes import FakePool  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"


def _principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["tool:invoke"], principal_type="agent")


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
        "required_scopes": ["tool:invoke", f"tool:{name}:invoke"],
        "tools": [{"name": "web_search", "description": "d", "input_schema": {"type": "object"}}],
    }


@pytest.mark.asyncio
async def test_list_tools_union_with_tenant_shadowing(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    # UNION: a platform tool-web-search + the tenant's OWN tool-web-search (shadows) +
    # a platform-only tool-translate.
    pool.on(
        "FROM tools t ORDER BY t.name",
        [
            {"tool_id": "plat-ws", "name": "tool-web-search", "tenant_id": None,
             "status": "active", "latest_version": "1.0.0", "is_platform": True},
            {"tool_id": "tenant-ws", "name": "tool-web-search", "tenant_id": TENANT,
             "status": "active", "latest_version": "2.0.0", "is_platform": False},
            {"tool_id": "plat-tr", "name": "tool-translate", "tenant_id": None,
             "status": "active", "latest_version": "1.0.0", "is_platform": True},
        ],
    )
    # Version + capability + health responders (shared across tools for simplicity).
    pool.on("FROM tool_versions WHERE tool_id = %s AND status = %s ORDER BY created_at",
            [{"version": "2.0.0", "manifest": _manifest("tool-web-search", "2.0.0", "http://t:8080"),
              "status": "active", "created_at": "now"}])
    pool.on("FROM tool_capabilities WHERE tool_id",
            [{"capability": "web_search", "required_scope": "tool:tool-web-search:invoke"}])
    pool.on("FROM tool_health WHERE tool_id", [{"status": "active", "last_etag": None,
            "consecutive_failures": 0, "last_polled": None}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/tools")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    by_name = {d["name"]: d for d in data}
    # Tenant priority: only ONE tool-web-search, and it's the tenant's.
    assert by_name["tool-web-search"]["owner"] == "tenant"
    assert "tool-translate" in by_name
    assert len(data) == 2
    # The RLS GUC was set to the Principal's tenant.
    assert pool.last_tenant == TENANT


@pytest.mark.asyncio
async def test_get_tool_by_name_resolves_latest_active(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "plat-ws", "name": "tool-web-search", "tenant_id": None,
              "status": "active", "latest_version": "1.2.0", "is_platform": True}])
    pool.on("ORDER BY created_at DESC LIMIT 1",
            [{"version": "1.2.0", "manifest": _manifest("tool-web-search", "1.2.0", "http://ws:8080"),
              "status": "active", "created_at": "now"}])
    pool.on("FROM tool_capabilities WHERE tool_id",
            [{"capability": "web_search", "required_scope": "tool:tool-web-search:invoke"}])
    pool.on("FROM tool_health WHERE tool_id", [{"status": "active"}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/tools/tool-web-search")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "tool-web-search"
    assert body["version"] == "1.2.0"
    assert body["invoke_url"] == "http://ws:8080"
    assert body["required_scopes"] == ["tool:invoke", "tool:tool-web-search:invoke"]
    assert body["owner"] == "platform"


@pytest.mark.asyncio
async def test_get_tool_version_pinning(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "t", "name": "tool-x", "tenant_id": TENANT,
              "status": "active", "latest_version": "3.0.0", "is_platform": False}])
    # The version-pinned query path: WHERE ... AND version = %s.
    pool.on("AND version = %s AND status = %s",
            [{"version": "1.0.0", "manifest": _manifest("tool-x", "1.0.0", "http://x:8080"),
              "status": "active", "created_at": "old"}])
    pool.on("FROM tool_capabilities WHERE tool_id", [])
    pool.on("FROM tool_health WHERE tool_id", [{"status": "active"}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/tools/tool-x", params={"version": "1.0.0"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_get_tool_unknown_name_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool().on("FROM tools WHERE name = %s", [])
    app.state.db_pool = pool
    resp = await ac.get("/v1/tools/nope")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_tool_missing_pinned_version_404(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "t", "name": "tool-x", "tenant_id": TENANT,
              "status": "active", "latest_version": "3.0.0", "is_platform": False}])
    pool.on("AND version = %s AND status = %s", [])  # no such active version
    app.state.db_pool = pool
    resp = await ac.get("/v1/tools/tool-x", params={"version": "9.9.9"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_discovery_503_when_no_db(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = None
    resp = await ac.get("/v1/tools")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
