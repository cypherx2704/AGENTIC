"""Per-agent tool-loop toggle (``tool_loop_enabled``, migration 0007) — CONTRACT suite.

Exercises the REAL ``/v1/agents/{id}/runtime`` HTTP surface (api/agents.py) to prove the
toggle survives the request/response contract end-to-end, with the repo layer monkeypatched
(this is the endpoint contract, not a live-SQL test — the SQL shape is guarded by the
integrity suite ``test_agent_tool_loop_toggle.py``):

  * PUT create ACCEPTS ``tool_loop_enabled: false`` in the body and the created 201 response
    echoes it (the body model parses + carries the field);
  * PUT create OMITTING the field DEFAULTS to true in the response (back-compat: an existing
    client that never sends the field keeps the prior "multiple request" behaviour);
  * PUT update ACCEPTS + persists the toggle (the value reaches the repo write layer);
  * GET read-back returns the field in the response body (the API view carries it);
  * an unknown field is still rejected 422 (extra=forbid on the body model is intact).

Harness mirrors ``test_wp08_agent_runtime.py`` (admin principal, stubbed Auth
cross-validation + cache bust, monkeypatched agents_repo).
"""

from __future__ import annotations

from typing import Any

from agent_runtime.api import agents as agents_api
from agent_runtime.db import agents_repo
from agent_runtime.models.agent import AgentRuntime
from agent_runtime.services.auth_client import AuthAgent

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"


def _runtime(**over: Any) -> AgentRuntime:
    base = AgentRuntime(
        agent_id=TEST_AGENT,
        tenant_id=TEST_TENANT,
        name="Test Agent",
        system_prompt="You are helpful.",
    )
    return base.model_copy(update=over)


def _install_admin_principal(app: Any) -> None:
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


def _stub_cross_validate(monkeypatch: Any) -> None:
    class _FakeAuth:
        async def get_agent(self, agent_id: str, **_kw: Any) -> AuthAgent:
            return AuthAgent(agent_id=agent_id, tenant_id=TEST_TENANT)

    monkeypatch.setattr(agents_api, "_auth_client", lambda request: _FakeAuth())


def _stub_cache(monkeypatch: Any) -> None:
    async def _noop(valkey: Any, settings: Any, agent_id: str) -> None:
        return None

    monkeypatch.setattr(agents_api.agent_config_cache, "invalidate", _noop)


def _body(**over: Any) -> dict[str, Any]:
    base = {"name": "Test Agent", "system_prompt": "You are helpful.", "status": "active",
            "runtime_version": "1.0.0"}
    base.update(over)
    return base


# ── PUT create: the body ACCEPTS tool_loop_enabled=false and the response echoes it ─────
async def test_put_create_accepts_and_echoes_toggle_false(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    _stub_cache(monkeypatch)

    captured: dict[str, Any] = {}

    async def _no_existing(pool: Any, tenant_id: str, agent_id: str) -> None:
        return None

    async def _insert(pool: Any, tenant_id: str, agent_id: str, body: Any) -> AgentRuntime:
        # The endpoint parsed the field onto the registration body and hands it to the repo.
        captured["tool_loop_enabled"] = body.tool_loop_enabled
        return _runtime(status=body.status, tool_loop_enabled=body.tool_loop_enabled)

    monkeypatch.setattr(agents_repo, "get_agent", _no_existing)
    monkeypatch.setattr(agents_repo, "insert_agent_runtime", _insert)

    resp = await client.put(
        f"/v1/agents/{TEST_AGENT}/runtime",
        json=_body(status="pending_config", tool_loop_enabled=False),
    )

    assert resp.status_code == 201, resp.text
    assert captured["tool_loop_enabled"] is False        # reached the repo write
    assert resp.json()["tool_loop_enabled"] is False     # echoed in the response contract


# ── PUT create OMITTING the field DEFAULTS true (back-compat for existing clients) ───────
async def test_put_create_defaults_true_when_field_omitted(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    _stub_cache(monkeypatch)

    captured: dict[str, Any] = {}

    async def _no_existing(pool: Any, tenant_id: str, agent_id: str) -> None:
        return None

    async def _insert(pool: Any, tenant_id: str, agent_id: str, body: Any) -> AgentRuntime:
        captured["tool_loop_enabled"] = body.tool_loop_enabled
        return _runtime(status=body.status, tool_loop_enabled=body.tool_loop_enabled)

    monkeypatch.setattr(agents_repo, "get_agent", _no_existing)
    monkeypatch.setattr(agents_repo, "insert_agent_runtime", _insert)

    # Body does NOT include tool_loop_enabled — an existing client's payload shape.
    resp = await client.put(
        f"/v1/agents/{TEST_AGENT}/runtime", json=_body(status="pending_config")
    )

    assert resp.status_code == 201, resp.text
    assert captured["tool_loop_enabled"] is True         # default preserved prior behaviour
    assert resp.json()["tool_loop_enabled"] is True


# ── PUT update ACCEPTS + persists the toggle (reaches the repo update layer) ─────────────
async def test_put_update_persists_toggle(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    _stub_cache(monkeypatch)

    captured: dict[str, Any] = {}

    async def _existing(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime:
        return _runtime(status="active", tool_loop_enabled=True)

    async def _update(pool: Any, tenant_id: str, agent_id: str, body: Any,
                      *, runtime_version: str, status: str) -> AgentRuntime:
        captured["tool_loop_enabled"] = body.tool_loop_enabled
        return _runtime(status=status, runtime_version=runtime_version,
                        tool_loop_enabled=body.tool_loop_enabled)

    monkeypatch.setattr(agents_repo, "get_agent", _existing)
    monkeypatch.setattr(agents_repo, "update_agent_runtime", _update)

    resp = await client.put(
        f"/v1/agents/{TEST_AGENT}/runtime", json=_body(status="active", tool_loop_enabled=False)
    )

    assert resp.status_code == 200, resp.text
    assert captured["tool_loop_enabled"] is False        # persisted via the update path
    assert resp.json()["tool_loop_enabled"] is False


# ── GET read-back returns the field in the response body ────────────────────────────────
async def test_get_returns_toggle(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()

    async def _existing(pool: Any, tenant_id: str, agent_id: str) -> AgentRuntime:
        return _runtime(status="active", tool_loop_enabled=False)

    monkeypatch.setattr(agents_repo, "get_agent", _existing)

    resp = await client.get(f"/v1/agents/{TEST_AGENT}/runtime")

    assert resp.status_code == 200, resp.text
    assert resp.json()["tool_loop_enabled"] is False


# ── extra=forbid still holds: an unknown field is rejected 422 ──────────────────────────
async def test_unknown_field_still_rejected(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _stub_cross_validate(monkeypatch)
    _stub_cache(monkeypatch)

    resp = await client.put(
        f"/v1/agents/{TEST_AGENT}/runtime", json=_body(bogus_field=1)
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
