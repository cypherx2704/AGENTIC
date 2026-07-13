"""WP08 — agent runtime-config GET/PUT lifecycle (api/agents.py).

  * PUT create (no existing row)        -> 201 + body's status/version;
  * PUT update (existing row)           -> version BUMP + valid status transition + 200;
  * PUT invalid status transition        -> 409 CONFLICT;
  * GET                                  -> config + status + runtime_version;
  * GET / PUT without an admin scope     -> 403 (scope enforcement);
  * PUT busts the agent-config cache key (invalidation asserted via a recording fake).

The DB layer (``agents_repo``) and the Auth cross-validation are monkeypatched: this is
the endpoint-logic surface (transition rules, version bump, cache bust, scope gate), not a
live-SQL test. The Auth ``get_agent`` cross-validation is stubbed to a same-tenant agent so
the write proceeds; a tenant mismatch path is covered too.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.api import agents as agents_api
from agent_runtime.core.errors import ErrorCode
from agent_runtime.db import agents_repo
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services.auth_client import AuthAgent

# These mirror the conftest principal injected by the ``client``/``principal`` fixtures.
TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _runtime(status: str = "active", version: str = "1.0.0", **over: Any) -> AgentRuntime:
    base = AgentRuntime(
        agent_id=TEST_AGENT,
        tenant_id=TEST_TENANT,
        name="Test Agent",
        runtime_version=version,
        status=status,
        system_prompt="You are helpful.",
    )
    return base.model_copy(update=over)


class _RecordingCache:
    """Records invalidate() calls so the PUT cache-bust can be asserted."""

    def __init__(self) -> None:
        self.invalidated: list[str] = []


def _install_admin_principal(app: Any) -> None:
    """Override require_principal to inject an ADMIN-scoped principal for this app.

    The conftest ``client`` fixture overrides require_principal with the default
    ``agent:execute`` principal; the runtime-management surface needs an admin scope, so we
    re-override it here (tests/ is not an importable package, so we build the Principal
    inline rather than importing the conftest helper).
    """
    from agent_runtime.core.auth import Principal, require_principal

    admin = Principal(
        tenant_id=TEST_TENANT,
        agent_id=TEST_AGENT,
        scopes=["agent:admin"],
        principal_type="agent",
        raw_token="test.inbound.agent-jwt",
        raw_claims={"tenant_id": TEST_TENANT, "agent_id": TEST_AGENT},
    )
    app.dependency_overrides[require_principal] = lambda: admin


def _stub_cross_validate(monkeypatch: Any, *, mismatch: bool = False) -> None:
    """Stub the Auth cross-validation to a same-tenant (or mismatched) agent."""

    class _FakeAuth:
        async def get_agent(self, agent_id: str, **_kw: Any) -> AuthAgent:
            tenant = "ZZ" if mismatch else TEST_TENANT
            return AuthAgent(agent_id=agent_id, tenant_id=tenant)

    monkeypatch.setattr(agents_api, "_auth_client", lambda request: _FakeAuth())


def _stub_cache(monkeypatch: Any, recorder: _RecordingCache) -> None:
    async def _fake_invalidate(valkey: Any, settings: Any, agent_id: str) -> None:
        recorder.invalidated.append(agent_id)

    monkeypatch.setattr(agents_api.agent_config_cache, "invalidate", _fake_invalidate)


def _body(status: str = "active", version: str = "1.0.0") -> dict[str, Any]:
    return {"name": "Test Agent", "system_prompt": "You are helpful.", "status": status,
            "runtime_version": version}


# ── PUT create -> 201 ───────────────────────────────────────────────────────────────
async def test_put_create_returns_201_and_busts_cache(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    cache = _RecordingCache()
    _stub_cache(monkeypatch, cache)

    async def _no_existing(pool: Any, tenant_id: str, agent_id: str) -> None:
        return None

    async def _insert(pool: Any, tenant_id: str, agent_id: str, body: Any, **_kw: Any) -> AgentRuntime:
        return _runtime(status=body.status, version=body.runtime_version)

    monkeypatch.setattr(agents_repo, "get_agent", _no_existing)
    monkeypatch.setattr(agents_repo, "insert_agent_runtime", _insert)

    resp = await client.put(f"/v1/agents/{TEST_AGENT}/runtime", json=_body(status="pending_config"))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending_config"
    assert body["runtime_version"] == "1.0.0"
    assert cache.invalidated == [TEST_AGENT]  # create busts the cache


# ── PUT update -> version bump + valid transition ───────────────────────────────────
async def test_put_update_bumps_version_on_valid_transition(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    cache = _RecordingCache()
    _stub_cache(monkeypatch, cache)

    captured: dict[str, Any] = {}

    async def _existing(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime:
        return _runtime(status="active", version="1.0.4")

    async def _update(
        pool: Any, tenant_id: str, agent_id: str, body: Any, *, runtime_version: str, status: str, **_kw: Any
    ) -> AgentRuntime:
        captured["runtime_version"] = runtime_version
        captured["status"] = status
        return _runtime(status=status, version=runtime_version)

    monkeypatch.setattr(agents_repo, "get_agent", _existing)
    monkeypatch.setattr(agents_repo, "update_agent_runtime", _update)

    # active -> inactive is a valid transition.
    resp = await client.put(f"/v1/agents/{TEST_AGENT}/runtime", json=_body(status="inactive"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "inactive"
    assert body["runtime_version"] == "1.0.5"  # PATCH bumped 1.0.4 -> 1.0.5
    assert captured == {"runtime_version": "1.0.5", "status": "inactive"}
    assert cache.invalidated == [TEST_AGENT]


# ── PUT invalid transition -> 409 ───────────────────────────────────────────────────
async def test_put_invalid_transition_returns_409(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    cache = _RecordingCache()
    _stub_cache(monkeypatch, cache)

    async def _existing(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime:
        return _runtime(status="active", version="1.0.0")

    called = {"update": False}

    async def _update(*_a: Any, **_k: Any) -> AgentRuntime:
        called["update"] = True
        return _runtime()

    monkeypatch.setattr(agents_repo, "get_agent", _existing)
    monkeypatch.setattr(agents_repo, "update_agent_runtime", _update)

    # active -> pending_config is a forbidden regression.
    resp = await client.put(f"/v1/agents/{TEST_AGENT}/runtime", json=_body(status="pending_config"))

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "CONFLICT"
    assert called["update"] is False  # rejected before the DB write
    assert cache.invalidated == []  # no bust on a rejected transition


# ── PUT tenant mismatch from Auth -> 403 ────────────────────────────────────────────
async def test_put_tenant_mismatch_returns_403(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch, mismatch=True)

    resp = await client.put(f"/v1/agents/{TEST_AGENT}/runtime", json=_body())

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ── GET -> config + version ─────────────────────────────────────────────────────────
async def test_get_runtime_returns_config(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()

    async def _existing(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime:
        assert tenant_id == TEST_TENANT  # RLS scope from the JWT
        return _runtime(status="active", version="2.3.1")

    monkeypatch.setattr(agents_repo, "get_agent", _existing)

    resp = await client.get(f"/v1/agents/{TEST_AGENT}/runtime")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == TEST_AGENT
    assert body["status"] == "active"
    assert body["runtime_version"] == "2.3.1"
    assert body["system_prompt"] == "You are helpful."


# ── GET unknown -> 404 ──────────────────────────────────────────────────────────────
async def test_get_runtime_missing_returns_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()

    async def _none(pool: Any, tenant_id: str, agent_id: str) -> None:
        return None

    monkeypatch.setattr(agents_repo, "get_agent", _none)

    resp = await client.get(f"/v1/agents/{TEST_AGENT}/runtime")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── scope enforcement: a non-admin principal -> 403 on GET and PUT ──────────────────
async def test_get_requires_admin_scope(client) -> None:  # type: ignore[no-untyped-def]
    # The default conftest principal carries only 'agent:execute' (not admin).
    app = client._transport.app
    app.state.db_pool = object()
    resp = await client.get(f"/v1/agents/{TEST_AGENT}/runtime")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_put_requires_admin_scope(client) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    resp = await client.put(f"/v1/agents/{TEST_AGENT}/runtime", json=_body())
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ── unit: the transition + version-bump helpers (the rules the endpoint enforces) ───
def test_status_transition_rules() -> None:
    from agent_runtime.models.agent import is_valid_status_transition

    assert is_valid_status_transition("pending_config", "active")
    assert is_valid_status_transition("active", "inactive")
    assert is_valid_status_transition("inactive", "active")
    assert is_valid_status_transition("active", "active")  # self-transition ok
    assert not is_valid_status_transition("active", "pending_config")  # regression denied
    assert not is_valid_status_transition("inactive", "pending_config")


def test_version_bump_rules() -> None:
    from agent_runtime.models.agent import bump_runtime_version

    assert bump_runtime_version("1.0.0") == "1.0.1"
    assert bump_runtime_version("2.3.9") == "2.3.10"
    assert bump_runtime_version("1.0") == "1.0.1"  # bare MAJOR.MINOR gets a patch
    assert bump_runtime_version("free-text") == "free-text"  # non-semver left untouched


# Guard against ErrorCode drift in the assertions above.
def test_error_codes_present() -> None:
    assert ErrorCode.CONFLICT
    assert ErrorCode.FORBIDDEN
    assert ErrorCode.NOT_FOUND
