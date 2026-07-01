"""Quota enforcement (413 count/storage, 429 rate) + fail-open."""

from __future__ import annotations

import pytest

from rag_service.core.config import Settings
from rag_service.services import quota

from .conftest import make_principal

AUTH = {"Authorization": "Bearer test"}

# A `limits` claim block keeps the test independent of the in-code plan defaults.
TINY_LIMITS = {
    "rag": {
        "kbs_max": 2,
        "documents_per_kb_max": 1,
        "queries_per_min": 2,
        "storage_bytes_max": 30 * 1024,  # ~1 chunk
    }
}


@pytest.mark.asyncio
async def test_kbs_max_413(app_client, auth_as) -> None:  # noqa: ANN001
    auth_as(make_principal(plan="free", limits=TINY_LIMITS))
    await app_client.post("/v1/kbs", json={"name": "a"}, headers=AUTH)
    await app_client.post("/v1/kbs", json={"name": "b"}, headers=AUTH)
    resp = await app_client.post("/v1/kbs", json={"name": "c"}, headers=AUTH)
    assert resp.status_code == 413
    body = resp.json()["error"]
    assert body["code"] == "QUOTA_EXCEEDED"
    assert body["details"]["dimension"] == "kbs_max"


@pytest.mark.asyncio
async def test_documents_per_kb_max_413(app_client, auth_as) -> None:  # noqa: ANN001
    auth_as(make_principal(plan="free", limits=TINY_LIMITS))
    kb = (await app_client.post("/v1/kbs", json={"name": "docs"}, headers=AUTH)).json()
    first = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={"name": "d1.md", "content": "hello", "source_type": "markdown"},
        headers=AUTH,
    )
    assert first.status_code == 201
    second = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/documents",
        json={"name": "d2.md", "content": "world", "source_type": "markdown"},
        headers=AUTH,
    )
    assert second.status_code == 413
    assert second.json()["error"]["details"]["dimension"] == "documents_per_kb_max"


@pytest.mark.asyncio
async def test_queries_per_min_429(app_client, auth_as) -> None:  # noqa: ANN001
    auth_as(make_principal(plan="free", limits=TINY_LIMITS))
    kb = (await app_client.post("/v1/kbs", json={"name": "rl"}, headers=AUTH)).json()
    body = {"query": "x", "min_score": 0.0}
    r1 = await app_client.post(f"/v1/kbs/{kb['kb_id']}/query", json=body, headers=AUTH)
    r2 = await app_client.post(f"/v1/kbs/{kb['kb_id']}/query", json=body, headers=AUTH)
    r3 = await app_client.post(f"/v1/kbs/{kb['kb_id']}/query", json=body, headers=AUTH)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert "Retry-After" in r3.headers


@pytest.mark.asyncio
async def test_quota_fails_open_without_plan_claim(app_client, auth_as) -> None:  # noqa: ANN001
    # No plan claim -> default tier (generous) -> not blocked.
    auth_as(make_principal(plan=None))
    for i in range(4):
        resp = await app_client.post("/v1/kbs", json={"name": f"kb{i}"}, headers=AUTH)
        assert resp.status_code == 201


def test_resolve_limits_reads_jwt_block() -> None:
    settings = Settings()
    p = make_principal(plan="pro", limits={"rag": {"kbs_max": 7}})
    limits = quota.resolve_limits(p, settings=settings)
    assert limits.kbs_max == 7  # JWT block overrides the plan default
    assert limits.plan == "pro"
