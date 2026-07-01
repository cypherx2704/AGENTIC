"""Consolidation / forgetting routine (opt-in) — candidate selection + soft-delete audit.

The routine never runs by default (no lifespan task started); these tests drive the pure
``run_consolidation_once`` against the in-memory repo to prove it (a) only picks low-
importance, old, valid memories and (b) soft-deletes them to the audit trail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from _helpers import TEST_TENANT
from memory_service.services import consolidation
from memory_service.services.repository import InMemoryRepository, new_memory


def _seed(repo: InMemoryRepository, content: str, *, importance: float, age_days: float) -> str:
    mem = new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="note", tags=[], content=content, metadata={}, vector=[0.0] * 1536, session_id=None,
        ttl_seconds=None, importance_score=importance,
    )
    created = datetime.now(UTC) - timedelta(days=age_days)
    mem.created_at = created
    mem.last_accessed_at = created
    mem.last_retrieved_at = created
    repo._memories[mem.id] = mem  # noqa: SLF001 — test seed
    return mem.id


@pytest.mark.asyncio
async def test_candidates_only_low_importance_and_old() -> None:
    repo = InMemoryRepository()
    keep_important = _seed(repo, "critical fact", importance=0.9, age_days=400)
    keep_recent = _seed(repo, "trivial but new", importance=0.05, age_days=1)
    forget = _seed(repo, "trivial and old", importance=0.05, age_days=400)

    cands = await repo.consolidation_candidates(
        max_importance=0.3, min_age_seconds=30 * 24 * 3600.0, batch_size=100,
    )
    ids = {m.id for m in cands}
    assert forget in ids
    assert keep_important not in ids  # too important
    assert keep_recent not in ids     # too recent


@pytest.mark.asyncio
async def test_run_consolidation_soft_deletes_to_audit() -> None:
    repo = InMemoryRepository()
    forget = _seed(repo, "trivial and old", importance=0.05, age_days=400)
    keep = _seed(repo, "important", importance=0.95, age_days=400)

    forgotten = await consolidation.run_consolidation_once(
        repo, max_importance=0.3, min_age_seconds=30 * 24 * 3600.0, batch_size=100,
    )
    assert forgotten == 1
    assert forget not in repo._memories  # noqa: SLF001
    assert keep in repo._memories         # noqa: SLF001
    assert any(a["action"] == "consolidated" and a["memory_id"] == forget for a in repo.audit)


@pytest.mark.asyncio
async def test_run_consolidation_no_candidates_noop() -> None:
    repo = InMemoryRepository()
    _seed(repo, "important recent", importance=0.95, age_days=1)
    forgotten = await consolidation.run_consolidation_once(
        repo, max_importance=0.3, min_age_seconds=30 * 24 * 3600.0, batch_size=100,
    )
    assert forgotten == 0
    assert repo.audit == []


def test_summarize_cluster() -> None:
    repo = InMemoryRepository()
    a = repo._memories  # noqa: SLF001
    from memory_service.services.repository import new_memory as nm

    mems = [
        nm(tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
           type="note", tags=[], content=f"fact {i}", metadata={}, vector=[0.0] * 1536,
           session_id=None, ttl_seconds=None)
        for i in range(3)
    ]
    summary = consolidation.summarize_cluster(mems)
    assert "consolidated 3 memories" in summary
    assert "fact 0" in summary
    assert isinstance(a, dict)
