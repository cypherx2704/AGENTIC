"""BUG 1 (+ MINOR) — API-layer validation of UUID path params + castable query filters.

The repos bind their string args straight onto Postgres ``uuid`` / ``timestamptz``
columns, so a non-UUID ``{task_id}`` / ``{agent_id}`` or a non-RFC-3339 ``?since`` used
to reach the DB as a raw uncastable string and surface as a generic 500 (and, on the
agents cross-validate write path, a misleading 503). ``core.validation`` now rejects
these at the edge BEFORE any repo / downstream call:

  * non-UUID PATH id        -> 404 NOT_FOUND  (cannot name any RLS-scoped row);
  * non-UUID ?agent_id      -> 422 VALIDATION_ERROR (a malformed filter is a client error);
  * non-RFC-3339 ?since     -> 422 VALIDATION_ERROR.

These run against the REAL routers. The repos are monkeypatched with a guard that FAILS
if ever called with a malformed value, proving the bad value never reaches the DB layer.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.api import agents as agents_api
from agent_runtime.core import validation
from agent_runtime.core.errors import ApiError, ErrorCode
from agent_runtime.db import agents_repo, tasks_repo
from agent_runtime.services.auth_client import AuthAgent

TEST_TENANT = "00000000-0000-0000-0000-0000000000aa"
TEST_AGENT = "00000000-0000-0000-0000-0000000000bb"
BAD_ID = "not-a-uuid"


def _install_admin_principal(app: Any) -> None:
    """Re-override require_principal with an ADMIN principal (agents surface is admin-only)."""
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


# ── unit: the validation helpers ────────────────────────────────────────────────────
def test_parse_uuid_path_accepts_canonical_unchanged() -> None:
    assert validation.parse_uuid_path(TEST_AGENT, param="Agent") == TEST_AGENT


def test_parse_uuid_path_rejects_non_uuid_as_404() -> None:
    with pytest.raises(ApiError) as ei:
        validation.parse_uuid_path(BAD_ID, param="Task")
    assert ei.value.code == ErrorCode.NOT_FOUND
    assert ei.value.status_code == 404


def test_parse_uuid_query_passthrough_none_and_canonical() -> None:
    assert validation.parse_uuid_query(None, param="agent_id") is None
    assert validation.parse_uuid_query(TEST_AGENT, param="agent_id") == TEST_AGENT


def test_parse_uuid_query_rejects_non_uuid_as_422() -> None:
    with pytest.raises(ApiError) as ei:
        validation.parse_uuid_query(BAD_ID, param="agent_id")
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.status_code == 422


def test_parse_rfc3339_query_passthrough_and_reject() -> None:
    assert validation.parse_rfc3339_query(None, param="since") is None
    # A valid instant is returned UNCHANGED (psycopg binds it to timestamptz directly).
    assert validation.parse_rfc3339_query("2026-06-10T00:00:00Z", param="since") == (
        "2026-06-10T00:00:00Z"
    )
    with pytest.raises(ApiError) as ei:
        validation.parse_rfc3339_query("yesterday", param="since")
    assert ei.value.code == ErrorCode.VALIDATION_ERROR


# ── BUG 1 — tasks endpoints: non-UUID {task_id} -> 404, never a 5xx ─────────────────
def _guard_get_task(monkeypatch: Any) -> None:
    async def _must_not_run(pool: Any, tenant_id: str, task_id: str) -> Any:
        raise AssertionError(f"repo.get_task reached with raw id {task_id!r}")

    monkeypatch.setattr(tasks_repo, "get_task", _must_not_run)


async def test_get_task_non_uuid_returns_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    _guard_get_task(monkeypatch)

    resp = await client.get(f"/v1/tasks/{BAD_ID}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_cancel_task_non_uuid_returns_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    _guard_get_task(monkeypatch)

    resp = await client.delete(f"/v1/tasks/{BAD_ID}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── BUG 1 — list filters: non-castable ?since / ?agent_id -> 422, never a 5xx ───────
def _guard_list_tasks(monkeypatch: Any) -> None:
    async def _must_not_run(pool: Any, tenant_id: str, **kwargs: Any) -> Any:
        raise AssertionError(f"repo.list_tasks reached with {kwargs!r}")

    monkeypatch.setattr(tasks_repo, "list_tasks", _must_not_run)


async def test_list_bad_since_returns_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    _guard_list_tasks(monkeypatch)

    resp = await client.get("/v1/tasks?since=not-a-timestamp")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_bad_agent_id_returns_422(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    app.state.db_pool = object()
    _guard_list_tasks(monkeypatch)

    resp = await client.get(f"/v1/tasks?agent_id={BAD_ID}")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ── MINOR — agents/{non-uuid}/runtime: was a misleading 503, now a clean 404 ────────
def _guard_agents_repo(monkeypatch: Any) -> None:
    async def _must_not_run(*a: Any, **k: Any) -> Any:
        raise AssertionError("agents_repo reached with a non-UUID id")

    monkeypatch.setattr(agents_repo, "get_agent", _must_not_run)


def _exploding_cross_validate(monkeypatch: Any) -> None:
    """Make the Auth cross-validate FAIL (503) if reached — the old (buggy) outcome.

    With the path-UUID guard in place the cross-validate is never reached for a bad id,
    so the test asserts 404 (not 503); this fake proves the guard short-circuits BEFORE
    the downstream Auth call that used to mis-map a bad id to SERVICE_UNAVAILABLE.
    """

    class _Boom:
        async def get_agent(self, agent_id: str, **_kw: Any) -> AuthAgent:
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Auth service returned 422.")

    monkeypatch.setattr(agents_api, "_auth_client", lambda request: _Boom())


async def test_get_runtime_non_uuid_returns_404(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _guard_agents_repo(monkeypatch)

    resp = await client.get(f"/v1/agents/{BAD_ID}/runtime")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_put_runtime_non_uuid_returns_404_not_503(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _guard_agents_repo(monkeypatch)
    _exploding_cross_validate(monkeypatch)  # would 503 if the bad id reached Auth

    resp = await client.put(
        f"/v1/agents/{BAD_ID}/runtime",
        json={"name": "x", "system_prompt": "y", "status": "active", "runtime_version": "1.0.0"},
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_post_runtime_non_uuid_returns_404_not_503(client, monkeypatch: Any) -> None:  # type: ignore[no-untyped-def]
    app = client._transport.app
    _install_admin_principal(app)
    app.state.db_pool = object()
    _guard_agents_repo(monkeypatch)
    _exploding_cross_validate(monkeypatch)

    resp = await client.post(
        f"/v1/agents/{BAD_ID}/runtime",
        json={"name": "x", "system_prompt": "y", "status": "active", "runtime_version": "1.0.0"},
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
