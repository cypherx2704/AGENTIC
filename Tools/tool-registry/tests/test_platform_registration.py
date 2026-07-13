"""Phase 5 · 5-registry — the PLATFORM (public) registration path + the retire/de-register path.

Two concerns, mirroring the existing ``test_api_registration`` idioms (real ASGI app, the
scope dependency exercised for real, a scripted ``FakePool`` standing in for Postgres):

1. **Platform registration** — ``POST /v1/platform/tools`` (+ ``/versions``) requires
   ``platform:admin`` (403 without) and persists a PLATFORM tool: the write runs under
   ``in_platform`` (an EMPTY ``app.tenant_id`` GUC, captured as ``last_tenant == ""``), so the
   shared ``NULLIF(current_setting('app.tenant_id', true), '')::uuid`` INSERT yields ``NULL``
   (``tenant_id NULL``) and ``visibility`` is forced ``public``. A tenant registration through
   ``POST /v1/tools`` still 400s on ``public`` — public is platform-only.
2. **Retire** — ``POST /v1/tools/{name}/retire`` flips ``tools.status='retired'`` (and retires
   active versions). It is scope-gated: a platform tool needs ``platform:admin``; a tenant tool
   needs the base admin scope and is RLS-scoped to the caller's tenant.
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


def _platform_admin() -> Principal:
    # A platform operator: carries a tenant_id claim (auth always does) but the platform path
    # runs under an empty GUC, so this tenant is NEVER used for the DB scope.
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["platform:admin"], principal_type="agent")


def _tool_admin() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["tool:admin"], principal_type="agent")


def _plain() -> Principal:
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


# ── (1) Platform registration ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_register_platform_tool_requires_platform_admin(make_client) -> None:  # type: ignore[no-untyped-def]
    """A tool:admin (tenant) principal cannot register a PLATFORM tool — needs platform:admin."""
    app, ac = await make_client(_tool_admin)
    app.state.db_pool = FakePool()
    resp = await ac.post("/v1/platform/tools", json=_manifest("tool-ws", "1.0.0"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_register_platform_tool_writes_tenant_null_public(make_client) -> None:  # type: ignore[no-untyped-def]
    """platform:admin registers a PLATFORM tool: empty GUC (tenant_id NULL) + visibility public."""
    app, ac = await make_client(_platform_admin)
    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "plat-id", "name": "tool-ws", "tenant_id": None, "status": "active",
              "latest_version": "1.0.0", "visibility": "public"}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/platform/tools", json=_manifest("tool-ws", "1.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "tool-ws"
    assert body["owner"] == "platform"
    assert body["visibility"] == "public"
    # The write ran under in_platform: an EMPTY GUC (=> tenant_id resolves NULL via the shared
    # NULLIF(current_setting('app.tenant_id', true), '')::uuid expression), NOT the caller tenant.
    assert pool.last_tenant == ""
    # visibility='public' was bound as the last INSERT param.
    insert = next(w for w in pool.writes if "INSERT INTO tools (" in w[0])
    assert insert[1][-1] == "public"
    # Version + capability rows were written too.
    assert any("INSERT INTO tool_versions" in w[0] for w in pool.writes)
    assert any("INSERT INTO tool_capabilities" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_register_platform_tool_conflict_409(make_client) -> None:  # type: ignore[no-untyped-def]
    from psycopg.errors import UniqueViolation

    app, ac = await make_client(_platform_admin)
    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "plat-id", "name": "tool-ws", "tenant_id": None, "status": "active",
              "latest_version": "1.0.0", "visibility": "public"}])

    def _dup(sql: str, params) -> None:  # type: ignore[no-untyped-def]
        if "INSERT INTO tools (" in sql:
            raise UniqueViolation("duplicate key")

    pool.write_hook = _dup
    app.state.db_pool = pool
    resp = await ac.post("/v1/platform/tools", json=_manifest("tool-ws", "1.0.0"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"


@pytest.mark.asyncio
async def test_tenant_still_cannot_create_public(make_client) -> None:  # type: ignore[no-untyped-def]
    """GOVERNANCE unchanged: a tenant registration via POST /v1/tools still 400s on 'public'."""
    app, ac = await make_client(_tool_admin)
    pool = FakePool()
    app.state.db_pool = pool
    manifest = _manifest("tool-mine", "1.0.0")
    manifest["visibility"] = "public"
    resp = await ac.post("/v1/tools", json=manifest)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # Rejected before any write.
    assert not any("INSERT INTO tools (" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_register_platform_version_success(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_platform_admin)
    pool = FakePool()
    # Resolve the platform tool (tenant_id IS NULL) for this name.
    pool.on("WHERE name = %s AND tenant_id IS NULL",
            [{"tool_id": "plat-id", "name": "tool-ws", "tenant_id": None, "status": "active",
              "latest_version": "1.0.0", "visibility": "public", "is_platform": True}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/platform/tools/tool-ws/versions", json=_manifest("tool-ws", "2.0.0"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == "2.0.0"
    assert body["owner"] == "platform"
    assert body["visibility"] == "public"
    # Versioning ran under in_platform (empty GUC) so the new rows stay tenant_id NULL.
    assert pool.last_tenant == ""
    assert any("INSERT INTO tool_versions" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_register_platform_version_unknown_tool_404(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_platform_admin)
    pool = FakePool().on("WHERE name = %s AND tenant_id IS NULL", [])  # no such platform tool
    app.state.db_pool = pool
    resp = await ac.post("/v1/platform/tools/ghost/versions", json=_manifest("ghost", "1.0.0"))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_register_platform_version_requires_platform_admin(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_tool_admin)
    app.state.db_pool = FakePool()
    resp = await ac.post("/v1/platform/tools/tool-ws/versions", json=_manifest("tool-ws", "2.0.0"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ── (2) Retire / de-register ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_retire_requires_admin_scope(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_plain)
    app.state.db_pool = FakePool()
    resp = await ac.post("/v1/tools/tool-mine/retire")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_retire_tenant_tool_flips_status(make_client) -> None:  # type: ignore[no-untyped-def]
    """tool:admin retires its OWN tenant tool: status->retired, active versions retired, tenant-scoped."""
    app, ac = await make_client(_tool_admin)
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "tid", "name": "tool-mine", "tenant_id": TENANT, "status": "active",
              "latest_version": "1.0.0", "visibility": "private", "is_platform": False}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/tools/tool-mine/retire")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"tool_id": "tid", "name": "tool-mine", "status": "retired", "owner": "tenant"}
    # Ran under the caller's tenant GUC (RLS-scoped to the owner).
    assert pool.last_tenant == TENANT
    # The tool status + its active versions were flipped to 'retired'.
    assert any("UPDATE tools SET status" in w[0] and w[1] == ("retired", "tid") for w in pool.writes)
    assert any(
        "UPDATE tool_versions SET status" in w[0] and w[1] == ("retired", "tid", "active")
        for w in pool.writes
    )


@pytest.mark.asyncio
async def test_retire_platform_tool_requires_platform_admin(make_client) -> None:  # type: ignore[no-untyped-def]
    """A tenant tool:admin cannot retire a PLATFORM tool — that needs platform:admin."""
    app, ac = await make_client(_tool_admin)
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "plat-id", "name": "tool-ws", "tenant_id": None, "status": "active",
              "latest_version": "1.0.0", "visibility": "public", "is_platform": True}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/tools/tool-ws/retire")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"
    # No write attempted — rejected on the scope gate.
    assert not any("UPDATE tools SET status" in w[0] for w in pool.writes)


@pytest.mark.asyncio
async def test_retire_platform_tool_with_platform_admin(make_client) -> None:  # type: ignore[no-untyped-def]
    """platform:admin retires a PLATFORM tool under in_platform (empty GUC)."""
    app, ac = await make_client(_platform_admin)
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s",
            [{"tool_id": "plat-id", "name": "tool-ws", "tenant_id": None, "status": "active",
              "latest_version": "1.0.0", "visibility": "public", "is_platform": True}])
    app.state.db_pool = pool

    resp = await ac.post("/v1/tools/tool-ws/retire")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "retired"
    assert body["owner"] == "platform"
    # Platform retire runs under an EMPTY GUC so the p_tools_platform policy admits it.
    assert pool.last_tenant == ""
    assert any("UPDATE tools SET status" in w[0] and w[1] == ("retired", "plat-id") for w in pool.writes)


@pytest.mark.asyncio
async def test_retire_unknown_tool_404(make_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = await make_client(_tool_admin)
    pool = FakePool().on("FROM tools WHERE name = %s", [])  # no such visible tool
    app.state.db_pool = pool
    resp = await ac.post("/v1/tools/ghost/retire")
    assert resp.status_code == 404, resp.text
