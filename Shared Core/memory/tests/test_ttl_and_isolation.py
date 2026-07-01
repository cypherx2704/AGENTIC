"""TTL sweep + tenant isolation (the RLS-equivalent predicate in the in-memory repo)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from _helpers import TEST_TENANT, bind_principal, make_principal
from memory_service.services import repository
from memory_service.services.repository import InMemoryRepository


@pytest.mark.asyncio
async def test_ttl_sweep_removes_only_expired() -> None:
    repo = InMemoryRepository()
    # One already-expired memory, one live.
    expired = repository.new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="note", tags=[], content="old", metadata={}, vector=[0.0], session_id=None,
        ttl_seconds=1,
    )
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=10)  # force-expire
    live = repository.new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="note", tags=[], content="fresh", metadata={}, vector=[0.0], session_id=None,
        ttl_seconds=None,
    )
    await repo.store(memory=expired, dedup_threshold=2.0, trace_id="t", producer_version="0.1.0")
    await repo.store(memory=live, dedup_threshold=2.0, trace_id="t", producer_version="0.1.0")

    swept = await repo.sweep_expired(batch_size=100)
    assert swept == 1
    count, _ = await repo.resource_usage(TEST_TENANT, "agent", "a")
    assert count == 1  # only the live one remains


@pytest.mark.asyncio
async def test_expired_memory_not_returned_by_search() -> None:
    repo = InMemoryRepository()
    m = repository.new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="note", tags=[], content="ghost", metadata={}, vector=[1.0, 0.0], session_id=None,
        ttl_seconds=1,
    )
    m.expires_at = datetime.now(UTC) - timedelta(seconds=10)
    await repo.store(memory=m, dedup_threshold=2.0, trace_id="t", producer_version="0.1.0")
    results = await repo.search(
        tenant_id=TEST_TENANT, caller_type="agent", caller_id="a", query_vector=[1.0, 0.0],
        top_k=10, type_filter=None, tags_filter=None, include_shared=True,
        user_scope_visibility="tenant",
    )
    assert results == []


@pytest.mark.asyncio
async def test_tenant_isolation_search_scoped_to_tenant(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    # Tenant 1 stores.
    bind_principal(app, make_principal(tenant_id="11111111-1111-1111-1111-111111111111"))
    await ac.post("/v1/memories", json={"content": "tenant one data", "scope": "tenant_shared"})

    # Tenant 2 searches the SAME content — must get nothing (cross-tenant isolation).
    bind_principal(app, make_principal(tenant_id="22222222-2222-2222-2222-222222222222"))
    s = await ac.post("/v1/memories/search", json={"query": "tenant one data", "top_k": 50})
    assert s.status_code == 200
    assert s.json()["count"] == 0
