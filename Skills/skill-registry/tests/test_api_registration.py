"""POST /v1/skills + POST /v1/skills/{name}/versions — registration, scope gate, retention.

Drives the real ASGI app. The admin-scope dependency is exercised for real (a token
without skill:admin/platform:admin is rejected 403); the DB is a scripted FakePool so
registration + version-retention behaviour is asserted without Postgres.
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


def _admin_principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["skill:admin"], principal_type="agent")


def _plain_principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["skill:invoke"], principal_type="agent")


def _manifest(name: str, version: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "protocol_version": "mcp/1.0",
        "name": name,
        "version": version,
        "description": "x",
        "base_url": "http://x:8080",
        "required_scopes": ["skill:invoke", f"skill:{name}:invoke"],
        "skills": [{"name": "do_thing", "description": "d", "input_schema": {"type": "object"}}],
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
async def test_register_skill_requires_admin_scope(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_plain_principal)
    app.state.db_pool = FakePool()
    resp = await ac.post("/v1/skills", json=_manifest("skill-mine", "1.0.0"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_register_skill_success(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    pool = FakePool()
    # The INSERT ... RETURNING skill row.
    pool.on("INSERT INTO skills",
            [{"skill_id": "new-id", "name": "skill-mine", "tenant_id": TENANT,
              "status": "active", "latest_version": "1.0.0"}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/skills", json=_manifest("skill-mine", "1.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "skill-mine"
    assert body["version"] == "1.0.0"
    assert body["owner"] == "tenant"
    # The tenant GUC was the Principal's; a version + capability row were written.
    assert pool.last_tenant == TENANT
    assert any("INSERT INTO skill_versions" in w[0] for w in pool.writes)
    assert any("INSERT INTO skill_capabilities" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_register_skill_invalid_manifest_400(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    app.state.db_pool = FakePool()
    bad = _manifest("Skill_Bad", "1.0.0")  # name not dash-case
    resp = await ac.post("/v1/skills", json=bad)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_register_version_retires_oldest_beyond_three(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    pool = FakePool()
    # Resolve the tenant's own skill.
    pool.on("FROM skills WHERE name = %s",
            [{"skill_id": "tid", "name": "skill-mine", "tenant_id": TENANT,
              "status": "active", "latest_version": "3.0.0", "is_platform": False}])
    # After inserting v4, the active-version listing (newest first) has FOUR — the
    # retention sweep must retire the oldest (last in the DESC list).
    pool.on("SELECT version FROM skill_versions WHERE skill_id = %s AND status = %s ORDER BY created_at DESC",
            [{"version": "4.0.0"}, {"version": "3.0.0"}, {"version": "2.0.0"}, {"version": "1.0.0"}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/skills/skill-mine/versions", json=_manifest("skill-mine", "4.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == "4.0.0"
    # Retention kept max 3 active -> retired the single oldest (1.0.0).
    assert body["retired_versions"] == ["1.0.0"]
    # That retirement was an UPDATE ... status='retired' on version 1.0.0.
    retire_writes = [w for w in pool.writes if "UPDATE skill_versions SET status" in w[0]]
    assert retire_writes and retire_writes[0][1] == ("retired", "tid", "1.0.0")


@pytest.mark.asyncio
async def test_register_version_unknown_skill_404(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    pool = FakePool().on("FROM skills WHERE name = %s", [])  # no such tenant skill
    app.state.db_pool = pool
    resp = await ac.post("/v1/skills/ghost/versions", json=_manifest("ghost", "1.0.0"))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_register_version_name_mismatch_400(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_admin_principal)
    app.state.db_pool = FakePool()
    # Path name 'skill-a' but manifest name 'skill-b'.
    resp = await ac.post("/v1/skills/skill-a/versions", json=_manifest("skill-b", "1.0.0"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
