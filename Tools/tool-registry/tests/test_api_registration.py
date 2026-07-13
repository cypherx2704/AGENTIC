"""POST /v1/tools + POST /v1/tools/{name}/versions — registration, scope gate, retention.

Drives the real ASGI app. The admin-scope dependency is exercised for real (a token
without tool:admin/platform:admin is rejected 403); the DB is a scripted FakePool so
registration + version-retention behaviour is asserted without Postgres.
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


def _admin_principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["tool:admin"], principal_type="agent")


def _plain_principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["tool:invoke"], principal_type="agent")


def _manifest(name: str, version: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "protocol_version": "mcp/1.0",
        "name": name,
        "version": version,
        "description": "x",
        "base_url": "http://x:8080",
        "required_scopes": ["tool:invoke", f"tool:{name}:invoke"],
        "tools": [{"name": "do_thing", "description": "d", "input_schema": {"type": "object"}}],
    }


@pytest_asyncio.fixture
async def make_client():  # type: ignore[no-untyped-def]
    created = []

    async def _factory(principal_factory):  # type: ignore[no-untyped-def]
        app = create_app()
        app.dependency_overrides[require_principal] = principal_factory
        mgr = LifespanManager(app, startup_timeout=15)
        await mgr.__aenter__()
        app.state.db_pool = None
        app.state.valkey = None
        app.state.http_client = None  # eager poll becomes a no-op
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        created.append((mgr, ac))
        return app, ac

    yield _factory
    for mgr, ac in created:
        await ac.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_register_tool_requires_admin_scope(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_plain_principal)
    app.state.db_pool = FakePool()
    resp = await ac.post("/v1/tools", json=_manifest("tool-mine", "1.0.0"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_register_tool_success(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    pool = FakePool()
    # The INSERT ... RETURNING tool row.
    pool.on("INSERT INTO tools",
            [{"tool_id": "new-id", "name": "tool-mine", "tenant_id": TENANT,
              "status": "active", "latest_version": "1.0.0"}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/tools", json=_manifest("tool-mine", "1.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "tool-mine"
    assert body["version"] == "1.0.0"
    assert body["owner"] == "tenant"
    # The tenant GUC was the Principal's; a version + capability row were written.
    assert pool.last_tenant == TENANT
    assert any("INSERT INTO tool_versions" in w[0] for w in pool.writes)
    assert any("INSERT INTO tool_capabilities" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_register_tool_invalid_manifest_400(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    app.state.db_pool = FakePool()
    bad = _manifest("Tool_Bad", "1.0.0")  # name not dash-case
    resp = await ac.post("/v1/tools", json=bad)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_register_version_retires_oldest_beyond_three(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    pool = FakePool()
    # Resolve the tenant's own tool.
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "tid", "name": "tool-mine", "tenant_id": TENANT,
              "status": "active", "latest_version": "3.0.0", "is_platform": False}])
    # After inserting v4, the active-version listing (newest first) has FOUR — the
    # retention sweep must retire the oldest (last in the DESC list).
    pool.on("SELECT version FROM tool_versions WHERE tool_id = %s AND status = %s ORDER BY created_at DESC",
            [{"version": "4.0.0"}, {"version": "3.0.0"}, {"version": "2.0.0"}, {"version": "1.0.0"}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/tools/tool-mine/versions", json=_manifest("tool-mine", "4.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == "4.0.0"
    # Retention kept max 3 active -> retired the single oldest (1.0.0).
    assert body["retired_versions"] == ["1.0.0"]
    # That retirement was an UPDATE ... status='retired' on version 1.0.0.
    retire_writes = [w for w in pool.writes if "UPDATE tool_versions SET status" in w[0]]
    assert retire_writes and retire_writes[0][1] == ("retired", "tid", "1.0.0")


@pytest.mark.asyncio
async def test_register_same_version_refreshes_in_place(make_client) -> None:  # type: ignore[no-untyped-def]
    """Re-registering an EXISTING (tool_id, version) refreshes the manifest + capabilities
    in place (the stable-version MCP auto-refresh path) instead of 409-ing on the dup key."""
    app, ac = await make_client(_admin_principal)
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "tid", "name": "tool-mine", "tenant_id": TENANT,
              "status": "active", "latest_version": "1.0.0", "is_platform": False}])
    # The version already exists -> take the in-place refresh branch.
    pool.on("SELECT 1 FROM tool_versions WHERE tool_id", [{"exists": 1}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/tools/tool-mine/versions", json=_manifest("tool-mine", "1.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == "1.0.0"
    assert body["retired_versions"] == []  # a refresh never churns the version chain
    # Refresh path: UPDATE the version's manifest + re-replace capabilities; NO new version row.
    assert any("UPDATE tool_versions SET manifest" in w[0] for w in pool.writes)
    assert any("DELETE FROM tool_capabilities" in w[0] for w in pool.writes)
    assert any("INSERT INTO tool_capabilities" in w[0] for w in pool.writes)
    assert not any("INSERT INTO tool_versions" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_register_version_unknown_tool_404(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    pool = FakePool().on("FROM tools WHERE name = %s", [])  # no such tenant tool
    app.state.db_pool = pool
    resp = await ac.post("/v1/tools/ghost/versions", json=_manifest("ghost", "1.0.0"))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_register_version_name_mismatch_400(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    app.state.db_pool = FakePool()
    # Path name 'tool-a' but manifest name 'tool-b'.
    resp = await ac.post("/v1/tools/tool-a/versions", json=_manifest("tool-b", "1.0.0"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
