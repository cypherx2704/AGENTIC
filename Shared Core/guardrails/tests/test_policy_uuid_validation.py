"""Systemic MED fix — non-UUID policy id (path) / agent_id (body) map to 404/422, not 500/503.

Before the fix the ``/v1/policies/{id}`` get + simulate handlers let a raw non-UUID path id
bind to a uuid column and 500-ed (uncaught), while edit/assign caught it under the broad
``except Exception`` and reported a misleading 503. A non-UUID ``agent_id`` in the assign body
took the same 503 path. This mirrors the up-front ``_parse_uuid`` guard already in
``api/violations.py`` (custom-rules endpoints use TEXT ids and are unaffected).

The fakes never need to RUN for the validation path — the guard rejects before any DB call —
so a scripted pool that records nothing is enough to prove the write never reached the DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from _fakedb import ScriptedPool
from guardrails_service.core.auth import Principal, require_principal
from guardrails_service.main import create_app
from guardrails_service.services.policy_engine import PolicyEngine

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"
GOOD_ID = "11111111-1111-1111-1111-111111111111"
BAD_ID = "not-a-uuid"


def _admin_principal() -> Principal:
    return Principal(
        tenant_id=TENANT, agent_id=AGENT,
        scopes=["guardrails:check", "tenant:admin"], principal_type="service",
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[require_principal] = _admin_principal
    async with LifespanManager(app, startup_timeout=15):
        # A scripted pool that returns no rows; the UUID guard must short-circuit first.
        pool = ScriptedPool(lambda q, p: None)
        app.state.db_pool = pool
        app.state.policy_engine = PolicyEngine(pool)  # type: ignore[arg-type]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ac._grd_pool = pool  # type: ignore[attr-defined]
            yield ac


# ── Path policy_id -> 404 NOT_FOUND ───────────────────────────────────────────────


async def test_get_non_uuid_policy_id_is_404(client: AsyncClient) -> None:
    resp = await client.get(f"/v1/policies/{BAD_ID}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    # The malformed id never reached the DB.
    assert not client._grd_pool.executed  # type: ignore[attr-defined]


async def test_simulate_non_uuid_policy_id_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/v1/policies/{BAD_ID}/simulate", json={"text": "hi"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert not client._grd_pool.executed  # type: ignore[attr-defined]


async def test_edit_non_uuid_policy_id_is_404(client: AsyncClient) -> None:
    resp = await client.put(
        f"/v1/policies/{BAD_ID}",
        json={"name": "p", "rules": [{"rule_id": "pii-email-v1"}]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert not client._grd_pool.executed  # type: ignore[attr-defined]


async def test_assign_non_uuid_policy_id_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/v1/policies/{BAD_ID}/assign", json={"agent_id": AGENT})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert not client._grd_pool.executed  # type: ignore[attr-defined]


# ── Body agent_id -> 422 VALIDATION_ERROR ─────────────────────────────────────────


async def test_assign_non_uuid_agent_id_is_422(client: AsyncClient) -> None:
    # Valid policy id (path) but a malformed agent_id in the body -> 422, not 503/500.
    resp = await client.post(f"/v1/policies/{GOOD_ID}/assign", json={"agent_id": BAD_ID})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["reason"] == "invalid_uuid"
    assert body["error"]["details"]["field"] == "agent_id"
    assert not client._grd_pool.executed  # type: ignore[attr-defined]


@pytest.mark.parametrize("good", [GOOD_ID, GOOD_ID.upper()])
async def test_valid_uuid_still_reaches_db(client: AsyncClient, good: str) -> None:
    # A well-formed (canonicalised) UUID passes the guard and reaches the DB seam, where the
    # scripted pool returns no row -> the engine's PolicyNotFoundError maps to a 404 (not the
    # guard's 404), proving the guard did NOT short-circuit a valid id.
    resp = await client.get(f"/v1/policies/{good}")
    assert resp.status_code == 404, resp.text
    assert client._grd_pool.executed  # type: ignore[attr-defined] — the DB WAS consulted
