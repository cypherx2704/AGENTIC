"""WP07 — GET /v1/violations: pagination, filtering, redaction-safe projection, RLS.

A scripted pool returns canned violation rows so we can assert keyset pagination
(``limit`` + ``after_id`` cursor -> ``next_cursor`` / ``has_more``), filter validation, the
redaction-SAFE field set, JWT-tenant scoping (the read runs inside ``in_tenant``, which sets
``app.tenant_id``), and the always-answerable empty page when no pool is wired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from _fakedb import ScriptedPool
from guardrails_service.core.auth import Principal, require_principal
from guardrails_service.main import create_app

TENANT = "00000000-0000-0000-0000-0000000000aa"
AGENT = "00000000-0000-0000-0000-0000000000bb"


def _principal() -> Principal:
    return Principal(
        tenant_id=TENANT, agent_id=AGENT, scopes=["guardrails:check"], principal_type="service"
    )


def _row(idx: int, decision: str = "block") -> tuple[Any, ...]:
    """A violations row in _SELECT_COLS order (api/violations._row_to_violation)."""
    uid = f"00000000-0000-0000-0000-0000000000{idx:02d}"
    return (
        uid,                       # id::text
        f"check-{idx}",            # check_id
        f"req-{idx}",              # request_id
        AGENT,                     # agent_id
        None,                      # task_id
        f"trace-{idx}",            # trace_id
        "policy-1",                # policy_id
        "input",                   # direction
        decision,                  # decision
        "pii-email-v1",            # rule_id
        "PII Email Detector",      # rule_name
        "medium",                  # severity
        "email",                   # category
        "[REDACTED:email:abcd1234]",  # matched_text — SAFE token, never raw PII
        datetime(2026, 6, 10, 12, idx, 0, tzinfo=UTC),  # created_at
    )


async def _make(pool: ScriptedPool | None) -> tuple[Any, AsyncClient, list[Any]]:
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    lm = LifespanManager(app, startup_timeout=15)
    await lm.__aenter__()
    app.state.db_pool = pool
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    return app, ac, [lm]


@pytest_asyncio.fixture
async def build() -> AsyncIterator[Any]:  # type: ignore[misc]
    created: list[tuple[AsyncClient, list[Any]]] = []

    async def _build(pool: ScriptedPool | None) -> AsyncClient:
        _, ac, mgrs = await _make(pool)
        created.append((ac, mgrs))
        return ac

    yield _build

    for ac, mgrs in reversed(created):
        await ac.aclose()
        for lm in mgrs:
            await lm.__aexit__(None, None, None)


async def test_no_pool_returns_empty_page(build: Any) -> None:
    ac = await build(None)
    resp = await ac.get("/v1/violations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"violations": [], "next_cursor": None, "has_more": False}


async def test_returns_redaction_safe_fields_only(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(1)])
    ac = await build(pool)
    resp = await ac.get("/v1/violations")
    assert resp.status_code == 200, resp.text
    v = resp.json()["violations"][0]
    expected = {
        "id", "check_id", "request_id", "agent_id", "task_id", "trace_id",
        "policy_id", "direction", "decision", "rule_id", "rule_name",
        "severity", "category", "matched", "created_at",
    }
    assert set(v.keys()) == expected
    # The SAFE matched value is a redaction token (never raw text / a column named *_text).
    assert v["matched"].startswith("[REDACTED:")
    assert "matched_text" not in v


async def test_tenant_scoping_sets_app_tenant_id(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(1)])
    ac = await build(pool)
    await ac.get("/v1/violations")
    # RLS: every read runs inside in_tenant, which sets app.tenant_id to the JWT tenant.
    set_cfg = [(q, p) for q, p in pool.executed if "set_config('app.tenant_id'" in q]
    assert set_cfg, "the read must set app.tenant_id (RLS scoping)"
    assert set_cfg[0][1] == (TENANT,)


async def test_pagination_has_more_and_next_cursor(build: Any) -> None:
    # limit=2 -> handler fetches limit+1 (3); 3 rows back means has_more=True and the
    # next_cursor is the id of the LAST returned (2nd) row.
    rows = [_row(1), _row(2), _row(3)]
    pool = ScriptedPool(lambda q, p: rows)
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"limit": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["violations"]) == 2  # the extra probe row is trimmed
    assert body["has_more"] is True
    assert body["next_cursor"] == body["violations"][-1]["id"]
    # The query fetched limit+1 (the bound last param is the probe size).
    sel = pool.find("FROM guardrails.violations")
    assert sel
    assert sel[-1][1][-1] == 3  # LIMIT bound == limit + 1


async def test_pagination_last_page_no_more(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(1)])  # 1 row < limit -> no more
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"limit": 50})
    body = resp.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None


async def test_after_id_cursor_adds_keyset_predicate(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(5)])
    ac = await build(pool)
    cursor = "00000000-0000-0000-0000-000000000099"
    resp = await ac.get("/v1/violations", params={"after_id": cursor, "limit": 10})
    assert resp.status_code == 200, resp.text
    sel = pool.find("FROM guardrails.violations")
    main = [s for s in sel if "ORDER BY created_at DESC" in s[0]]
    assert main
    q, params = main[-1]
    assert "(created_at, id) <" in q  # keyset continuation predicate
    assert cursor in params  # the cursor id is bound


async def test_decision_filter_is_bound(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(1)])
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"decision": "block"})
    assert resp.status_code == 200, resp.text
    sel = [s for s in pool.find("FROM guardrails.violations") if "ORDER BY" in s[0]]
    q, params = sel[-1]
    assert "decision = %s" in q
    assert "block" in params


async def test_agent_filter_is_bound(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(1)])
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"agent_id": AGENT})
    assert resp.status_code == 200, resp.text
    sel = [s for s in pool.find("FROM guardrails.violations") if "ORDER BY" in s[0]]
    q, params = sel[-1]
    assert "agent_id = %s" in q
    assert AGENT in params


async def test_from_to_timestamps_bound(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [_row(1)])
    ac = await build(pool)
    resp = await ac.get(
        "/v1/violations",
        params={"from": "2026-06-01T00:00:00Z", "to": "2026-06-09T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    sel = [s for s in pool.find("FROM guardrails.violations") if "ORDER BY" in s[0]]
    q, _ = sel[-1]
    assert "created_at >= %s" in q
    assert "created_at < %s" in q


async def test_invalid_decision_is_400(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [])
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"decision": "explode"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["details"]["reason"] == "invalid_decision"


async def test_invalid_agent_uuid_is_400(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [])
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"agent_id": "not-a-uuid"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["details"]["reason"] == "invalid_uuid"


async def test_invalid_timestamp_is_400(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [])
    ac = await build(pool)
    resp = await ac.get("/v1/violations", params={"from": "yesterday"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["details"]["reason"] == "invalid_timestamp"


async def test_limit_out_of_range_is_422(build: Any) -> None:
    pool = ScriptedPool(lambda q, p: [])
    ac = await build(pool)
    # FastAPI Query(le=200) validation -> 422 for limit above the max.
    resp = await ac.get("/v1/violations", params={"limit": 500})
    assert resp.status_code == 422, resp.text


async def test_db_error_surfaces_503(build: Any) -> None:
    from _fakedb import FailingPool

    ac = await build(FailingPool())
    resp = await ac.get("/v1/violations")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
