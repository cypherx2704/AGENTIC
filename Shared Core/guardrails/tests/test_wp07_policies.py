"""WP07 — policy authoring CRUD + assignment (``/v1/policies``).

These tests exercise the DB-backed authoring surface against a scripted in-memory pool
(``tests/_fakedb.ScriptedPool``) injected onto ``app.state`` AFTER lifespan startup — the
same fake-pool + ``LifespanManager`` pattern as ``test_trace_request_id``. The pool records
every executed ``(sql, params)`` so we can assert the version chain, the audit/outbox
emissions, and JWT-only tenant scoping (Contract 13). Save-time validation (422) needs no
pool; the 503-without-pool posture is covered too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from _fakedb import ScriptedPool
from guardrails_service.core.auth import Principal, require_principal
from guardrails_service.main import create_app
from guardrails_service.services.policy_engine import PolicyEngine

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
ROOT_ID = "11111111-1111-1111-1111-111111111111"
V1_ID = "22222222-2222-2222-2222-222222222222"
V2_ID = "33333333-3333-3333-3333-333333333333"


def _principal(scopes: list[str]) -> Principal:
    return Principal(
        tenant_id=TENANT, agent_id=AGENT, scopes=scopes, principal_type="service"
    )


def _admin_principal() -> Principal:
    return _principal(["guardrails:check", "tenant:admin"])


def _reader_principal() -> Principal:
    return _principal(["guardrails:check"])


@pytest_asyncio.fixture
async def admin_client_factory() -> AsyncIterator[Any]:  # type: ignore[misc]
    """Yields a builder: ``await build(pool, principal_factory=...) -> AsyncClient``."""
    managers: list[Any] = []

    async def build(
        pool: ScriptedPool | None, principal_factory: Any = _admin_principal
    ) -> AsyncClient:
        app = create_app()
        app.dependency_overrides[require_principal] = principal_factory
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        managers.append(lm)
        # Inject the scripted pool + a DB-bound PolicyEngine post-startup.
        app.state.db_pool = pool
        app.state.policy_engine = PolicyEngine(pool)  # type: ignore[arg-type]
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        managers.append(ac)
        return ac

    yield build

    for obj in reversed(managers):
        if isinstance(obj, AsyncClient):
            await obj.aclose()
        else:
            await obj.__aexit__(None, None, None)


# ── Save-time validation (422) — no pool needed ─────────────────────────────────


async def test_create_unknown_rule_id_is_422(admin_client_factory: Any) -> None:
    pool = ScriptedPool()
    ac = await admin_client_factory(pool)
    resp = await ac.post(
        "/v1/policies",
        json={"name": "p", "rules": [{"rule_id": "does-not-exist-v9"}]},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    issues = body["error"]["details"]["issues"]
    assert any(i["reason"] == "unknown_rule" for i in issues)
    # Validation precedes any DB write.
    assert not pool.ran("INSERT INTO guardrails.policies")


async def test_create_invalid_stream_mode_is_422(admin_client_factory: Any) -> None:
    pool = ScriptedPool()
    ac = await admin_client_factory(pool)
    resp = await ac.post(
        "/v1/policies",
        json={
            "name": "p",
            "rules": [{"rule_id": "pii-email-v1"}],
            "stream_mode": "streaming",
        },
    )
    assert resp.status_code == 422, resp.text
    issues = resp.json()["error"]["details"]["issues"]
    assert any(i["field"] == "stream_mode" for i in issues)


async def test_create_empty_rules_is_422(admin_client_factory: Any) -> None:
    pool = ScriptedPool()
    ac = await admin_client_factory(pool)
    resp = await ac.post("/v1/policies", json={"name": "p", "rules": []})
    assert resp.status_code == 422, resp.text


# ── Scope enforcement (403) + missing-pool (503) ────────────────────────────────


async def test_create_requires_write_scope_403(admin_client_factory: Any) -> None:
    pool = ScriptedPool()
    ac = await admin_client_factory(pool, principal_factory=_reader_principal)
    resp = await ac.post(
        "/v1/policies", json={"name": "p", "rules": [{"rule_id": "pii-email-v1"}]}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_create_without_pool_is_503(admin_client_factory: Any) -> None:
    ac = await admin_client_factory(None)
    resp = await ac.post(
        "/v1/policies", json={"name": "p", "rules": [{"rule_id": "pii-email-v1"}]}
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


# ── Create (201) — tenant from JWT, audit + outbox emitted ───────────────────────


def _create_responder(query: str, params: Any) -> list[tuple[Any, ...]] | None:
    if "INSERT INTO guardrails.policies" in query and "RETURNING policy_id" in query:
        return [{"policy_id": V1_ID}]  # type: ignore[list-item]
    if "SELECT policy_id::text, root_policy_id::text" in query:
        # _fetch_version_row dict_row shape.
        return [  # type: ignore[list-item]
            {
                "policy_id": V1_ID,
                "root_policy_id": ROOT_ID,
                "tenant_id": params[0] if isinstance(params, tuple) else TENANT,
                "name": "My Policy",
                "version": 1,
                "status": "active",
                "rules": [{"rule_id": "pii-email-v1", "enabled": True}],
                "is_default": False,
                "stream_mode": "buffer",
                "fail_mode_override": None,
                "previous_policy_id": None,
                "created_at": None,
                "updated_at": None,
            }
        ]
    return None


async def test_create_201_uses_jwt_tenant_and_audits(admin_client_factory: Any) -> None:
    pool = ScriptedPool(_create_responder)
    ac = await admin_client_factory(pool)
    resp = await ac.post(
        "/v1/policies",
        json={
            "name": "My Policy",
            # A body tenant_id MUST be ignored (Contract 13) — extra="forbid" on the model
            # means it is rejected, so we omit it and instead assert the persisted tenant.
            "rules": [{"rule_id": "pii-email-v1"}],
        },
    )
    assert resp.status_code == 201, resp.text
    policy = resp.json()["policy"]
    assert policy["policy_id"] == ROOT_ID
    assert policy["version"] == 1

    # The INSERT bound the JWT tenant (params[0]), never a body value.
    inserts = pool.find("INSERT INTO guardrails.policies")
    assert inserts, "policy INSERT must run"
    assert inserts[0][1][0] == TENANT
    # Audit row + policy.changed outbox event both written in the same txn.
    assert pool.ran("INSERT INTO guardrails.policy_audit")
    assert pool.ran("INSERT INTO guardrails.outbox")


async def test_create_sets_root_to_self(admin_client_factory: Any) -> None:
    pool = ScriptedPool(_create_responder)
    ac = await admin_client_factory(pool)
    resp = await ac.post(
        "/v1/policies", json={"name": "My Policy", "rules": [{"rule_id": "pii-email-v1"}]}
    )
    assert resp.status_code == 201, resp.text
    # v1 is its own root: UPDATE ... SET root_policy_id = policy_id.
    assert pool.ran("SET root_policy_id = policy_id")


# ── Edit (PUT) — new version + fail_mode_override audit ──────────────────────────


def _edit_responder(query: str, params: Any) -> list[tuple[Any, ...]] | None:
    if "FROM guardrails.policies" in query and "FOR UPDATE" in query:
        # The current active version being superseded.
        return [  # type: ignore[list-item]
            {
                "policy_id": V1_ID,
                "version": 1,
                "tenant_id": TENANT,
                "is_default": False,
                "fail_mode_override": None,
            }
        ]
    if "INSERT INTO guardrails.policies" in query and "RETURNING policy_id" in query:
        return [{"policy_id": V2_ID}]  # type: ignore[list-item]
    if "SELECT policy_id::text, root_policy_id::text" in query:
        return [  # type: ignore[list-item]
            {
                "policy_id": V2_ID,
                "root_policy_id": ROOT_ID,
                "tenant_id": TENANT,
                "name": "Edited",
                "version": 2,
                "status": "active",
                "rules": [{"rule_id": "pii-email-v1", "enabled": True}],
                "is_default": False,
                "stream_mode": "buffer",
                "fail_mode_override": "open",
                "previous_policy_id": V1_ID,
                "created_at": None,
                "updated_at": None,
            }
        ]
    return None


async def test_edit_creates_new_version_chain(admin_client_factory: Any) -> None:
    pool = ScriptedPool(_edit_responder)
    ac = await admin_client_factory(pool)
    resp = await ac.put(
        f"/v1/policies/{ROOT_ID}",
        json={
            "name": "Edited",
            "rules": [{"rule_id": "pii-email-v1"}],
            "fail_mode_override": "open",
        },
    )
    assert resp.status_code == 200, resp.text
    policy = resp.json()["policy"]
    assert policy["version"] == 2
    assert policy["previous_policy_id"] == V1_ID
    assert policy["policy_id"] == ROOT_ID  # public id stays the stable root

    # The prior active version is superseded (append-only; never mutated in place).
    assert pool.ran("SET status = 'superseded'")
    # A fail_mode_override CHANGE (None -> open) emits its own audit + bus event.
    audits = pool.find("INSERT INTO guardrails.policy_audit")
    actions = [p[3] for _, p in audits]  # action is the 4th column
    assert "fail_mode_override_changed" in actions
    assert "edited" in actions


async def test_edit_unknown_policy_is_404(admin_client_factory: Any) -> None:
    def _none(query: str, params: Any) -> list[tuple[Any, ...]] | None:
        return None  # FOR UPDATE select returns nothing -> PolicyNotFoundError

    pool = ScriptedPool(_none)
    ac = await admin_client_factory(pool)
    resp = await ac.put(
        f"/v1/policies/{ROOT_ID}",
        json={"name": "Edited", "rules": [{"rule_id": "pii-email-v1"}]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_edit_platform_policy_is_403(admin_client_factory: Any) -> None:
    def _platform(query: str, params: Any) -> list[tuple[Any, ...]] | None:
        if "FOR UPDATE" in query:
            return [  # type: ignore[list-item]
                {
                    "policy_id": V1_ID,
                    "version": 1,
                    "tenant_id": None,  # platform policy
                    "is_default": True,
                    "fail_mode_override": None,
                }
            ]
        return None

    pool = ScriptedPool(_platform)
    ac = await admin_client_factory(pool)
    resp = await ac.put(
        f"/v1/policies/{ROOT_ID}",
        json={"name": "Edited", "rules": [{"rule_id": "pii-email-v1"}]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ── Get (active + versions) ──────────────────────────────────────────────────────


async def test_get_returns_active_and_versions(admin_client_factory: Any) -> None:
    def _chain(query: str, params: Any) -> list[tuple[Any, ...]] | None:
        if "ORDER BY version DESC" in query:
            return [  # type: ignore[list-item]
                {
                    "policy_id": V2_ID, "root_policy_id": ROOT_ID, "tenant_id": TENANT,
                    "name": "P", "version": 2, "status": "active",
                    "rules": [], "is_default": False, "stream_mode": "buffer",
                    "fail_mode_override": None, "previous_policy_id": V1_ID,
                    "created_at": None, "updated_at": None,
                },
                {
                    "policy_id": V1_ID, "root_policy_id": ROOT_ID, "tenant_id": TENANT,
                    "name": "P", "version": 1, "status": "superseded",
                    "rules": [], "is_default": False, "stream_mode": "buffer",
                    "fail_mode_override": None, "previous_policy_id": None,
                    "created_at": None, "updated_at": None,
                },
            ]
        return None

    pool = ScriptedPool(_chain)
    ac = await admin_client_factory(pool, principal_factory=_reader_principal)
    resp = await ac.get(f"/v1/policies/{ROOT_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"]["version"] == 2
    assert body["active"]["status"] == "active"
    assert len(body["versions"]) == 2
    assert body["active"]["policy_id"] == ROOT_ID


async def test_get_unknown_policy_is_404(admin_client_factory: Any) -> None:
    pool = ScriptedPool(lambda q, p: None)
    ac = await admin_client_factory(pool, principal_factory=_reader_principal)
    resp = await ac.get(f"/v1/policies/{ROOT_ID}")
    assert resp.status_code == 404, resp.text


# ── Assign (atomic agent repoint) ───────────────────────────────────────────────


async def test_assign_repoints_agent_and_upserts(admin_client_factory: Any) -> None:
    def _assign(query: str, params: Any) -> list[tuple[Any, ...]] | None:
        if "FROM guardrails.policies" in query and "status = 'active'" in query:
            return [{"tenant_id": TENANT, "policy_id": V1_ID}]  # type: ignore[list-item]
        return None

    pool = ScriptedPool(_assign)
    ac = await admin_client_factory(pool)
    resp = await ac.post(f"/v1/policies/{ROOT_ID}/assign", json={"agent_id": AGENT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assignment"]["agent_id"] == AGENT
    assert body["assignment"]["policy_id"] == ROOT_ID
    # Atomic UPSERT onto agent_policies + audit + outbox event.
    assert pool.ran("INSERT INTO guardrails.agent_policies")
    assert pool.ran("ON CONFLICT (agent_id, tenant_id)")
    assert pool.ran("INSERT INTO guardrails.policy_audit")


async def test_assign_unknown_policy_is_404(admin_client_factory: Any) -> None:
    pool = ScriptedPool(lambda q, p: None)
    ac = await admin_client_factory(pool)
    resp = await ac.post(f"/v1/policies/{ROOT_ID}/assign", json={"agent_id": AGENT})
    assert resp.status_code == 404, resp.text


async def test_assign_requires_write_scope_403(admin_client_factory: Any) -> None:
    pool = ScriptedPool()
    ac = await admin_client_factory(pool, principal_factory=_reader_principal)
    resp = await ac.post(f"/v1/policies/{ROOT_ID}/assign", json={"agent_id": AGENT})
    assert resp.status_code == 403, resp.text


# ── List (read-only built-in fallback when no pool) ─────────────────────────────


async def test_list_without_pool_returns_builtin(admin_client_factory: Any) -> None:
    ac = await admin_client_factory(None, principal_factory=_reader_principal)
    resp = await ac.get("/v1/policies")
    assert resp.status_code == 200, resp.text
    policies = resp.json()["policies"]
    assert policies
    assert policies[0]["name"] == "Platform Default Policy"
    assert policies[0]["is_default"] is True
