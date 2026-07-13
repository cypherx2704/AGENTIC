"""Server-wide default-access resolution for restricted tools (Phase 5).

Covers the NEW behaviour where a restricted tool carries its OWN default access mode
(``restricted_default`` — ``none`` unless the publisher chose ``ask``/``automated``) that
every tenant agent inherits when it has no explicit per-agent access row:

  * restricted + default ``ask``  + NO agent row -> ``ask``   (callable via HIL, no enrolment)
  * restricted + default ``none`` + NO agent row -> ``none``  (blocked by default)
  * NOT restricted               + NO agent row -> ``automated``
  * an explicit per-agent row ALWAYS wins over the server-wide default.

Two complementary layers, both without live infra (per the repo's fakes + db_pool=None
degradation convention):

1. **Decision boundary** — drive the real ``queries.resolve_agent_tool_access`` through a
   scripted ``FakePool``. With no ``agent_tool_access`` responder the query returns no row,
   so the function must fall back to ``restricted_default``/``automated``. A scripted row
   proves the explicit grant shadows the default.

2. **HTTP endpoints** — drive the real ASGI app (auth dependency overridden, FakePool
   scripted) through ``GET /v1/tools/{name}/access`` and ``POST /v1/restricted-tools/{name}``
   so the endpoint wiring (get_restricted_default -> resolve_agent_tool_access) is exercised
   end to end.
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
from tool_registry.db import queries  # noqa: E402
from tool_registry.main import create_app  # noqa: E402

from .fakes import FakePool  # noqa: E402

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000a1"


# ── (1) Decision boundary — queries.resolve_agent_tool_access ─────────────────────
@pytest.mark.asyncio
async def test_restricted_default_ask_when_no_agent_row() -> None:
    """Restricted tool, server-wide default 'ask', agent has NO explicit row -> 'ask'."""
    pool = FakePool()  # no agent_tool_access responder -> the lookup returns no row
    mode = await queries.resolve_agent_tool_access(
        pool, TENANT,
        agent_id=AGENT, tool_server_name="tool-danger", capability=None,
        is_restricted=True, restricted_default="ask",
    )
    assert mode == "ask"
    # The RLS GUC was set from the tenant argument (never a body/query param).
    assert pool.last_tenant == TENANT


@pytest.mark.asyncio
async def test_restricted_default_none_when_no_agent_row() -> None:
    """Restricted tool, server-wide default 'none', no agent row -> 'none' (blocked)."""
    pool = FakePool()
    mode = await queries.resolve_agent_tool_access(
        pool, TENANT,
        agent_id=AGENT, tool_server_name="tool-danger", capability=None,
        is_restricted=True, restricted_default="none",
    )
    assert mode == "none"


@pytest.mark.asyncio
async def test_unrestricted_defaults_to_automated() -> None:
    """A tool that is NOT restricted defaults to 'automated' regardless of restricted_default."""
    pool = FakePool()
    mode = await queries.resolve_agent_tool_access(
        pool, TENANT,
        agent_id=AGENT, tool_server_name="tool-open", capability=None,
        is_restricted=False,  # restricted_default left at its 'none' default: must be ignored
    )
    assert mode == "automated"


@pytest.mark.asyncio
async def test_explicit_agent_row_overrides_restricted_default() -> None:
    """An explicit per-agent access row wins over the server-wide restricted default."""
    pool = FakePool()
    # An explicit server-wide grant for THIS agent (tool_capability IS NULL) overrides the
    # tool's own 'ask' default with 'automated'.
    pool.on(
        "FROM tools.agent_tool_access",
        [{"access_mode": "automated", "tool_capability": None}],
    )
    mode = await queries.resolve_agent_tool_access(
        pool, TENANT,
        agent_id=AGENT, tool_server_name="tool-danger", capability=None,
        is_restricted=True, restricted_default="ask",
    )
    assert mode == "automated"


# ── (2) HTTP endpoints — GET /access + POST /restricted-tools/{name} ──────────────
def _tenant_admin_principal() -> Principal:
    # tenant:admin covers both the (scopeless) GET /access and the tenant-admin POST guard;
    # agent_id is the default target agent for GET /access when ?agent_id= is omitted.
    return Principal(
        tenant_id=TENANT, agent_id=AGENT,
        scopes=["tenant:admin", "tool:invoke"], principal_type="agent",
    )


@pytest_asyncio.fixture
async def app_client():  # type: ignore[no-untyped-def]
    app = create_app()
    app.dependency_overrides[require_principal] = _tenant_admin_principal
    async with LifespanManager(app, startup_timeout=15):
        app.state.db_pool = None
        app.state.valkey = None
        app.state.http_client = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield app, ac


def _tool_row(name: str = "tool-danger", *, platform: bool = True) -> dict:
    return {
        "tool_id": "tid-danger", "name": name,
        "tenant_id": None if platform else TENANT,
        "status": "active", "latest_version": "1.0.0", "is_platform": platform,
    }


@pytest.mark.asyncio
async def test_get_access_returns_restricted_default_ask(app_client) -> None:  # type: ignore[no-untyped-def]
    """GET /access: restricted tool defaulting to 'ask', agent has no grant -> ask + restricted."""
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s", [_tool_row()])
    # get_restricted_default: the tool is restricted with a server-wide 'ask' default.
    pool.on("FROM tools.restricted_tools WHERE tool_id", [{"default_access_mode": "ask"}])
    # resolve_agent_tool_access: no explicit per-agent row (no agent_tool_access responder).
    app.state.db_pool = pool

    resp = await ac.get("/v1/tools/tool-danger/access")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_mode"] == "ask"
    assert body["restricted"] is True
    assert body["agent_id"] == AGENT
    assert body["tool"] == "tool-danger"
    assert pool.last_tenant == TENANT


@pytest.mark.asyncio
async def test_get_access_unrestricted_tool_is_automated(app_client) -> None:  # type: ignore[no-untyped-def]
    """GET /access: an unrestricted tool resolves to 'automated' with restricted=False."""
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s", [_tool_row("tool-open")])
    # get_restricted_default returns no row -> not restricted (default 'automated').
    pool.on("FROM tools.restricted_tools WHERE tool_id", [])
    app.state.db_pool = pool

    resp = await ac.get("/v1/tools/tool-open/access")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_mode"] == "automated"
    assert body["restricted"] is False


@pytest.mark.asyncio
async def test_get_access_explicit_grant_overrides_default(app_client) -> None:  # type: ignore[no-untyped-def]
    """GET /access: an explicit per-agent grant shadows the tool's 'ask' server default."""
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s", [_tool_row()])
    pool.on("FROM tools.restricted_tools WHERE tool_id", [{"default_access_mode": "ask"}])
    pool.on("FROM tools.agent_tool_access",
            [{"access_mode": "automated", "tool_capability": None}])
    app.state.db_pool = pool

    resp = await ac.get("/v1/tools/tool-danger/access", params={"agent_id": AGENT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The explicit grant wins even though the tool is restricted with an 'ask' default.
    assert body["access_mode"] == "automated"
    assert body["restricted"] is True


@pytest.mark.asyncio
async def test_mark_restricted_sets_server_default(app_client) -> None:  # type: ignore[no-untyped-def]
    """POST /restricted-tools/{name} persists the chosen server-wide default_access_mode."""
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s", [_tool_row()])
    app.state.db_pool = pool

    resp = await ac.post("/v1/restricted-tools/tool-danger",
                         json={"reason": "PII egress", "default_access_mode": "ask"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["restricted"] is True
    assert body["default_access_mode"] == "ask"
    assert body["tool"] == "tool-danger"
    # The restriction was written with the tool_id + reason + chosen default mode (params
    # order: tool_id, reason, default_access_mode).
    inserts = [w for w in pool.writes if "INSERT INTO tools.restricted_tools" in w[0]]
    assert inserts, "expected a restricted_tools INSERT"
    assert inserts[0][1] == ("tid-danger", "PII egress", "ask")
    assert pool.last_tenant == TENANT


@pytest.mark.asyncio
async def test_mark_restricted_rejects_bad_default_mode(app_client) -> None:  # type: ignore[no-untyped-def]
    """POST /restricted-tools/{name} validates default_access_mode against none|ask|automated."""
    app, ac = app_client
    pool = FakePool()
    pool.on("FROM tools WHERE name = %s", [_tool_row()])
    app.state.db_pool = pool

    resp = await ac.post("/v1/restricted-tools/tool-danger",
                         json={"reason": "x", "default_access_mode": "sometimes"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
