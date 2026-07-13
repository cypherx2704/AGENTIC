"""Tool visibility layer (Phase 1A) — column + backfill, the ?visibility= filter, ToolView.

Three concerns, mirroring the existing test idioms:

1. **Migration** — the shipped ``20260712_0007__tool_visibility.sql`` is parsed straight out
   of the file (like ``test_rls_cross_tenant``) so the column/CHECK/backfill contract can't
   drift, and we assert it does NOT weaken the existing ``tools`` RLS policies.
2. **Filter** — drives the real ASGI app (auth overridden, FakePool scripted) and asserts
   ``GET /v1/tools?visibility=`` narrows to the requested Marketplace sections; an invalid
   token 422s.
3. **ToolView** — visibility is threaded from the tool row into the discovery view (pure) and
   surfaced by the API; registration reads it from the manifest (default private) and persists.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://tool_user:localdev@localhost:5432/cypherx_platform")
os.environ.setdefault("SEED_PLATFORM_TOOLS", "false")

from tool_registry.core.auth import Principal, require_principal  # noqa: E402
from tool_registry.db import queries  # noqa: E402
from tool_registry.main import create_app  # noqa: E402
from tool_registry.services import discovery  # noqa: E402

from .fakes import FakePool, _Responder  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"

_MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"
_MIGRATION = (_MIGRATIONS / "20260712_0007__tool_visibility.sql").read_text(encoding="utf-8")


# ── (1) Migration: column + CHECK + backfill, RLS untouched ────────────────────
def test_migration_adds_visibility_column_not_null_default_private() -> None:
    pattern = re.compile(
        r"ADD COLUMN IF NOT EXISTS\s+visibility\s+VARCHAR\(15\)\s+NOT NULL\s+DEFAULT\s+'private'"
    )
    assert pattern.search(_MIGRATION), "visibility column must be NOT NULL DEFAULT 'private'"


def test_migration_check_constraint_allows_the_three_values() -> None:
    pattern = re.compile(
        r"CHECK\s*\(\s*visibility IN\s*\(\s*'private'\s*,\s*'protected'\s*,\s*'public'\s*\)\s*\)"
    )
    assert pattern.search(_MIGRATION), "CHECK must pin visibility to private|protected|public"


def test_migration_backfills_platform_rows_to_public() -> None:
    # Platform rows (tenant_id IS NULL) ARE the public rows; tenant rows stay private.
    pattern = re.compile(
        r"UPDATE tools\.tools\s+SET visibility\s*=\s*'public'\s+WHERE tenant_id IS NULL",
    )
    assert pattern.search(_MIGRATION), "backfill must flip platform (tenant_id NULL) rows public"


def test_migration_does_not_weaken_tools_rls_policies() -> None:
    """The visibility migration must NOT create/drop/alter any RLS policy (labels != RLS)."""
    assert "CREATE POLICY" not in _MIGRATION
    assert "DROP POLICY" not in _MIGRATION
    assert "ALTER POLICY" not in _MIGRATION


def _backfilled_visibility(tenant_id: str | None, current: str) -> str:
    """Model the backfill UPDATE: SET visibility='public' WHERE tenant_id IS NULL."""
    return "public" if tenant_id is None else current


def test_backfill_semantics_platform_public_tenant_private() -> None:
    assert _backfilled_visibility(None, "private") == "public"   # platform row -> public
    assert _backfilled_visibility(TENANT, "private") == "private"  # tenant row unchanged


# ── (2)+(3) App-driven: filter + visibility in the ToolView ────────────────────
def _principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["tool:invoke"], principal_type="agent")


def _admin_principal() -> Principal:
    return Principal(tenant_id=TENANT, agent_id=None, scopes=["tool:admin"], principal_type="agent")


def _manifest(name: str, version: str, base: str = "http://x:8080") -> dict:
    return {
        "schema_version": "1.0.0",
        "protocol_version": "mcp/1.0",
        "name": name,
        "version": version,
        "description": "x",
        "base_url": base,
        "required_scopes": ["tool:invoke", f"tool:{name}:invoke"],
        "tools": [{"name": "do_thing", "description": "d", "input_schema": {"type": "object"}}],
    }


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


def _scripted_pool() -> FakePool:
    """A pool returning three tools across all three visibilities, + shared version/cap/health."""
    pool = FakePool()
    # Token is "FROM tools t" (not "... ORDER BY t.name") so it matches BOTH the unfiltered SQL
    # and the filtered SQL (which inserts a WHERE t.visibility = ANY(%s) before the ORDER BY).
    pool.on(
        "FROM tools t",
        [
            {"tool_id": "plat-ws", "name": "tool-web-search", "tenant_id": None, "status": "active",
             "latest_version": "1.0.0", "visibility": "public", "is_platform": True},
            {"tool_id": "tenant-a", "name": "tool-alpha", "tenant_id": TENANT, "status": "active",
             "latest_version": "1.0.0", "visibility": "private", "is_platform": False},
            {"tool_id": "tenant-b", "name": "tool-beta", "tenant_id": TENANT, "status": "active",
             "latest_version": "1.0.0", "visibility": "protected", "is_platform": False},
        ],
    )
    pool.on("FROM tool_versions WHERE tool_id = %s AND status = %s ORDER BY created_at",
            [{"version": "1.0.0", "manifest": _manifest("tool-x", "1.0.0"),
              "status": "active", "created_at": "now"}])
    pool.on("FROM tool_capabilities WHERE tool_id",
            [{"capability": "do_thing", "required_scope": "tool:tool-x:invoke"}])
    pool.on("FROM tool_health WHERE tool_id", [{"status": "active"}])
    return pool


@pytest.mark.asyncio
async def test_list_tools_includes_visibility_in_view(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _scripted_pool()
    resp = await ac.get("/v1/tools")
    assert resp.status_code == 200, resp.text
    by_name = {d["name"]: d for d in resp.json()["data"]}
    assert by_name["tool-web-search"]["visibility"] == "public"
    assert by_name["tool-alpha"]["visibility"] == "private"
    assert by_name["tool-beta"]["visibility"] == "protected"


@pytest.mark.asyncio
async def test_visibility_filter_single_value(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _scripted_pool()
    resp = await ac.get("/v1/tools", params={"visibility": "public"})
    assert resp.status_code == 200, resp.text
    names = {d["name"] for d in resp.json()["data"]}
    assert names == {"tool-web-search"}


@pytest.mark.asyncio
async def test_visibility_filter_comma_separated(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _scripted_pool()
    resp = await ac.get("/v1/tools", params={"visibility": "private,protected"})
    assert resp.status_code == 200, resp.text
    names = {d["name"] for d in resp.json()["data"]}
    assert names == {"tool-alpha", "tool-beta"}


@pytest.mark.asyncio
async def test_no_filter_returns_all_visible(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _scripted_pool()
    resp = await ac.get("/v1/tools")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 3


@pytest.mark.asyncio
async def test_invalid_visibility_filter_422(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    app.state.db_pool = _scripted_pool()
    resp = await ac.get("/v1/tools", params={"visibility": "public,secret"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ── (2b) FIX 9: the visibility filter is pushed INTO the SQL (before the LIMIT) ─────
def test_filtered_list_sql_carries_visibility_predicate_unfiltered_unchanged() -> None:
    """The filtered query carries ``visibility = ANY(%s)`` BEFORE the LIMIT (so the cap counts
    only the requested visibility); the unfiltered query is unchanged (no predicate)."""
    from tool_registry.db.queries import _LIST_TOOLS_SQL, _LIST_TOOLS_SQL_FILTERED

    assert "visibility = ANY(%s)" in _LIST_TOOLS_SQL_FILTERED
    assert "LIMIT %s" in _LIST_TOOLS_SQL_FILTERED
    # Predicate must precede the LIMIT so the row cap counts filtered rows, not a post-LIMIT slice.
    assert _LIST_TOOLS_SQL_FILTERED.index("visibility = ANY") < _LIST_TOOLS_SQL_FILTERED.index("LIMIT")
    # Unfiltered path is unchanged: no WHERE / visibility predicate.
    assert "WHERE" not in _LIST_TOOLS_SQL
    assert "visibility = ANY" not in _LIST_TOOLS_SQL


@pytest.mark.asyncio
async def test_list_visible_tools_filtered_binds_predicate_unfiltered_does_not() -> None:
    """A ?visibility= request routes to the predicated SQL (bound with the visibility set + limit);
    the no-filter path executes the predicate-free SQL."""
    seen: list[tuple[str, object]] = []

    def _record(sql: str) -> bool:
        # Capture the executed SELECT so we can assert the predicate is present + bound.
        seen.append((" ".join(sql.split()), None))
        return "FROM tools t" in sql

    pool = FakePool()
    pool.responders.append(
        _Responder(
            _record,
            [{"tool_id": "plat-ws", "name": "tool-web-search", "tenant_id": None, "status": "active",
              "latest_version": "1.0.0", "visibility": "public", "is_platform": True}],
            once=False,
        )
    )

    # Filtered: the predicated SQL is used.
    rows = await queries.list_visible_tools(pool, TENANT, limit=7, visibility={"public"})
    assert rows and rows[0]["name"] == "tool-web-search"
    assert any("visibility = ANY(%s)" in sql for sql, _ in seen), seen

    # Unfiltered: the predicate-free SQL is used (unchanged path).
    seen.clear()
    await queries.list_visible_tools(pool, TENANT, limit=7)
    assert seen and all("visibility = ANY" not in sql for sql, _ in seen)


# ── (3) discovery.build_tool_view carries visibility ───────────────────────────
def test_build_tool_view_includes_visibility() -> None:
    view = discovery.build_tool_view(
        {"tool_id": "t", "name": "tool-x", "tenant_id": TENANT, "status": "active",
         "visibility": "protected", "is_platform": False},
        manifest={"name": "tool-x"},
        resolved_version="1.0.0",
        capabilities=[],
        health=None,
    )
    assert view["visibility"] == "protected"
    assert view["owner"] == "tenant"


# ── (3) registration reads visibility from the manifest + persists it ──────────
@pytest_asyncio.fixture
async def admin_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _admin_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        app.state.valkey = None
        app.state.http_client = None  # eager poll no-op
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


@pytest.mark.asyncio
async def test_register_defaults_visibility_private(admin_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = admin_client
    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "new-id", "name": "tool-mine", "tenant_id": TENANT, "status": "active",
              "latest_version": "1.0.0", "visibility": "private"}])
    app.state.db_pool = pool
    resp = await ac.post("/v1/tools", json=_manifest("tool-mine", "1.0.0"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["visibility"] == "private"
    # The INSERT bound visibility='private' as the last param.
    insert = next(w for w in pool.writes if "INSERT INTO tools (" in w[0])
    assert insert[1][-1] == "private"


@pytest.mark.asyncio
async def test_register_reads_visibility_from_manifest(admin_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = admin_client
    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "new-id", "name": "tool-mine", "tenant_id": TENANT, "status": "active",
              "latest_version": "1.0.0", "visibility": "protected"}])
    app.state.db_pool = pool
    manifest = _manifest("tool-mine", "1.0.0")
    manifest["visibility"] = "protected"
    resp = await ac.post("/v1/tools", json=manifest)
    assert resp.status_code == 201, resp.text
    assert resp.json()["visibility"] == "protected"
    insert = next(w for w in pool.writes if "INSERT INTO tools (" in w[0])
    assert insert[1][-1] == "protected"


@pytest.mark.asyncio
async def test_register_rejects_bad_manifest_visibility_400(admin_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = admin_client
    app.state.db_pool = FakePool()
    manifest = _manifest("tool-mine", "1.0.0")
    manifest["visibility"] = "secret"
    resp = await ac.post("/v1/tools", json=manifest)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_register_rejects_tenant_declared_public_400(admin_client) -> None:  # type: ignore[no-untyped-def]
    """GOVERNANCE: a tenant tool cannot be published 'public' — public is platform-only.

    Public is reached solely by admin promotion into the platform (tenant_id NULL) namespace.
    A tenant registration requesting 'public' must be rejected (not silently downgraded) so the
    caller learns the correct path. The DB CHECK (visibility<>'public' OR tenant_id IS NULL)
    backstops this; here we assert the API rejects it before any write.
    """
    app, ac = admin_client
    pool = FakePool()
    app.state.db_pool = pool
    manifest = _manifest("tool-mine", "1.0.0")
    manifest["visibility"] = "public"
    resp = await ac.post("/v1/tools", json=manifest)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # Rejected before persistence — no tools INSERT was attempted.
    assert not any("INSERT INTO tools (" in w[0] for w in pool.writes)


def test_migration_backstops_public_to_platform_rows() -> None:
    """The migration adds a CHECK so a tenant-owned row can never be persisted 'public'."""
    pattern = re.compile(
        r"CHECK\s*\(\s*visibility\s*<>\s*'public'\s+OR\s+tenant_id IS NULL\s*\)", re.IGNORECASE
    )
    assert pattern.search(_MIGRATION), "migration must backstop public==platform at the DB"


@pytest.mark.asyncio
async def test_create_tool_with_version_binds_visibility_param() -> None:
    """The query threads visibility into the INSERT so tools.tools carries the label."""
    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "t", "name": "tool-a", "tenant_id": TENANT, "status": "active",
              "latest_version": "1.0.0", "visibility": "protected"}])
    await queries.create_tool_with_version(
        pool, TENANT, name="tool-a", version="1.0.0",
        manifest={"name": "tool-a"}, capabilities=[], visibility="protected",
    )
    insert = next(w for w in pool.writes if "INSERT INTO tools (" in w[0])
    assert insert[1] == ("tool-a", "1.0.0", "protected")
