"""WP07 — POST /v1/redaction-keys/rotate: scope, persistence, 30-day grace demotion.

A scripted pool lets us assert that rotation (1) demotes the prior ``current`` key to
``retired`` with ``retired_at = NOW()`` (the grace clock) and (2) inserts a fresh
``current`` row — in one tenant transaction — and that ``tenant:admin`` is enforced. The
no-pool posture is 503 (nothing to rotate against). The pure rotation helper is also tested.
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
from guardrails_service.services.redaction_keys import rotate_key

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
NEW_KEY_ID = "44444444-4444-4444-4444-444444444444"


def _admin() -> Principal:
    return Principal(
        tenant_id=TENANT,
        agent_id=AGENT,
        scopes=["guardrails:check", "tenant:admin"],
        principal_type="service",
    )


def _non_admin() -> Principal:
    return Principal(
        tenant_id=TENANT, agent_id=AGENT, scopes=["guardrails:check"], principal_type="service"
    )


def _rotate_responder(query: str, params: Any) -> list[tuple[Any, ...]] | None:
    if "INSERT INTO guardrails.tenant_redaction_keys" in query and "RETURNING" in query:
        # key_ref is params[1] (the generated or BYO ref).
        ref = params[1] if isinstance(params, tuple) and len(params) > 1 else "env:GENERATED"
        return [(NEW_KEY_ID, ref, "current")]
    return None


@pytest_asyncio.fixture
async def build() -> AsyncIterator[Any]:  # type: ignore[misc]
    created: list[tuple[AsyncClient, Any]] = []

    async def _build(pool: ScriptedPool | None, principal_factory: Any = _admin) -> AsyncClient:
        app = create_app()
        app.dependency_overrides[require_principal] = principal_factory
        lm = LifespanManager(app, startup_timeout=15)
        await lm.__aenter__()
        app.state.db_pool = pool
        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        created.append((ac, lm))
        return ac

    yield _build

    for ac, lm in reversed(created):
        await ac.aclose()
        await lm.__aexit__(None, None, None)


async def test_rotate_requires_tenant_admin_403(build: Any) -> None:
    pool = ScriptedPool(_rotate_responder)
    ac = await build(pool, principal_factory=_non_admin)
    resp = await ac.post("/v1/redaction-keys/rotate", json={})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_rotate_without_pool_503(build: Any) -> None:
    ac = await build(None)
    resp = await ac.post("/v1/redaction-keys/rotate", json={})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


async def test_rotate_demotes_prior_and_inserts_current(build: Any) -> None:
    pool = ScriptedPool(_rotate_responder)
    ac = await build(pool)
    resp = await ac.post("/v1/redaction-keys/rotate", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rotated"] is True
    assert body["tenant_id"] == TENANT
    assert body["key_id"] == NEW_KEY_ID
    assert body["status"] == "current"
    assert body["grace_days"] == 30  # default redaction_key_grace_days

    # Prior current demoted to retired WITH retired_at (the grace clock starts).
    demotes = pool.find("SET status = 'retired', retired_at = NOW()")
    assert demotes, "prior current key must be demoted to retired with retired_at"
    assert demotes[0][1] == (TENANT,)  # scoped to the JWT tenant
    # A new current row is inserted in the SAME tenant transaction.
    assert pool.ran("INSERT INTO guardrails.tenant_redaction_keys")
    set_cfg = [(q, p) for q, p in pool.executed if "set_config('app.tenant_id'" in q]
    assert set_cfg and set_cfg[0][1] == (TENANT,)


async def test_rotate_accepts_byo_env_key_ref(build: Any) -> None:
    pool = ScriptedPool(_rotate_responder)
    ac = await build(pool)
    resp = await ac.post(
        "/v1/redaction-keys/rotate", json={"key_ref": "env:GRD_TENANT_KEY"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["key_ref"] == "env:GRD_TENANT_KEY"
    inserts = pool.find("INSERT INTO guardrails.tenant_redaction_keys")
    assert any("env:GRD_TENANT_KEY" in (p or ()) for _, p in inserts)


async def test_rotate_rejects_unsupported_key_ref_scheme_400(build: Any) -> None:
    pool = ScriptedPool(_rotate_responder)
    ac = await build(pool)
    resp = await ac.post(
        "/v1/redaction-keys/rotate", json={"key_ref": "ftp://evil"}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ── Pure rotate_key helper ───────────────────────────────────────────────────────


async def test_rotate_key_helper_returns_current() -> None:
    pool = ScriptedPool(_rotate_responder)
    result = await rotate_key(pool, TENANT)  # type: ignore[arg-type]
    assert result["key_id"] == NEW_KEY_ID
    assert result["status"] == "current"
    # Generated env: ref when none supplied.
    assert result["key_ref"].startswith("env:")
    # Demote ran before the insert (single transaction, demote-then-insert order).
    queries = [q for q, _ in pool.executed]
    demote_i = next(i for i, q in enumerate(queries) if "status = 'retired'" in q)
    insert_i = next(i for i, q in enumerate(queries) if "INSERT INTO guardrails.tenant_redaction_keys" in q)
    assert demote_i < insert_i


async def test_rotate_key_helper_uses_byo_ref() -> None:
    pool = ScriptedPool(_rotate_responder)
    result = await rotate_key(pool, TENANT, key_ref="sealed:blob123")  # type: ignore[arg-type]
    assert result["key_ref"] == "sealed:blob123"
