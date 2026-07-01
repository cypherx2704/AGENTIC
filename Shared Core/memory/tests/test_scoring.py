"""Composite retrieval scoring (Generative Agents) — pure-function + repo behavior.

The composite is OFF by default; these tests cover the math directly (no lifespan) plus the
in-memory repo's re-rank when scoring is enabled, asserting the candidate SET is unchanged
(only order differs) so the flag stays additive + non-breaking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from _helpers import TEST_TENANT
from memory_service.services import scoring
from memory_service.services.repository import InMemoryRepository, new_memory
from memory_service.services.scoring import ScoringWeights, composite_score


# ── Pure functions ───────────────────────────────────────────────────────────────────
def test_clamp01() -> None:
    assert scoring.clamp01(-0.5) == 0.0
    assert scoring.clamp01(1.5) == 1.0
    assert scoring.clamp01(0.3) == 0.3


def test_relevance_from_cosine() -> None:
    assert scoring.relevance_from_cosine(1.0) == 1.0
    assert scoring.relevance_from_cosine(-1.0) == 0.0
    assert scoring.relevance_from_cosine(0.0) == 0.5


def test_recency_decay_half_life() -> None:
    now = datetime.now(UTC)
    # Fresh -> 1.0
    assert scoring.recency_score(reference=now, now=now, half_life_seconds=3600) == 1.0
    # One half-life -> ~0.5
    old = now - timedelta(seconds=3600)
    assert round(scoring.recency_score(reference=old, now=now, half_life_seconds=3600), 3) == 0.5
    # None reference / non-positive half-life -> 1.0 (decay disabled)
    assert scoring.recency_score(reference=None, now=now, half_life_seconds=3600) == 1.0
    assert scoring.recency_score(reference=old, now=now, half_life_seconds=0.0) == 1.0


def test_heuristic_importance_in_range_and_keyworded_higher() -> None:
    trivial = scoring.heuristic_importance("hi", memory_type="note")
    salient = scoring.heuristic_importance(
        "Remember: this is important, my name is Alex, never share my password.",
        memory_type="fact",
    )
    assert 0.0 <= trivial <= 1.0
    assert 0.0 <= salient <= 1.0
    assert salient > trivial
    assert scoring.heuristic_importance("") == 0.0


def test_composite_bounded_and_weighting() -> None:
    now = datetime.now(UTC)
    w = ScoringWeights()
    # Perfect on every component -> 1.0
    assert composite_score(cosine=1.0, importance=1.0, reference=now, now=now, weights=w) == 1.0
    # Zero on every component -> 0.0
    old = now - timedelta(days=3650)
    val = composite_score(
        cosine=-1.0, importance=0.0, reference=old, now=now,
        weights=ScoringWeights(recency_half_life_seconds=3600),
    )
    assert 0.0 <= val <= 0.01
    # Degenerate zero weights -> falls back to pure relevance.
    rel_only = composite_score(
        cosine=1.0, importance=0.0, reference=old, now=now,
        weights=ScoringWeights(recency=0, importance=0, relevance=0),
    )
    assert rel_only == 1.0


# ── Repo re-rank behavior ──────────────────────────────────────────────────────────────
def _mk(repo: InMemoryRepository, content: str, vector: list[float], *, importance: float,
        age_days: float) -> str:
    mem = new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="fact", tags=[], content=content, metadata={}, vector=vector, session_id=None,
        ttl_seconds=None, importance_score=importance,
    )
    created = datetime.now(UTC) - timedelta(days=age_days)
    mem.created_at = created
    mem.last_accessed_at = created
    mem.last_retrieved_at = created
    repo._memories[mem.id] = mem  # noqa: SLF001 — test seed
    return mem.id


@pytest.mark.asyncio
async def test_composite_rerank_promotes_important_recent_same_set() -> None:
    repo = InMemoryRepository()
    # Two near-parallel vectors: 'stale' slightly CLOSER to the query than 'current'.
    q = [1.0, 0.0] + [0.0] * 1534
    stale = [1.0, 0.02] + [0.0] * 1534      # marginally higher cosine
    current = [0.97, 0.24] + [0.0] * 1534   # slightly lower cosine
    stale_id = _mk(repo, "old value", stale, importance=0.05, age_days=400)
    current_id = _mk(repo, "current value", current, importance=0.95, age_days=1)

    common = {
        "tenant_id": TEST_TENANT, "caller_type": "agent", "caller_id": "a", "query_vector": q,
        "top_k": 2, "type_filter": None, "tags_filter": None, "include_shared": True,
        "user_scope_visibility": "isolated",
    }
    cosine_order = [m.id for m in await repo.search(**common, scoring_enabled=False)]
    composite_order = [m.id for m in await repo.search(
        **common, scoring_enabled=True, scoring_weights=ScoringWeights(),
    )]

    # SAME candidate set (additive guarantee: only the order changes).
    assert set(cosine_order) == {stale_id, current_id} == set(composite_order)
    # Cosine ranks the stale distractor first; composite recovers the current one.
    assert cosine_order[0] == stale_id
    assert composite_order[0] == current_id


@pytest.mark.asyncio
async def test_default_path_is_pure_cosine_unchanged() -> None:
    repo = InMemoryRepository()
    q = [1.0, 0.0] + [0.0] * 1534
    a = _mk(repo, "closer", [1.0, 0.0] + [0.0] * 1534, importance=0.0, age_days=999)
    b = _mk(repo, "farther", [0.5, 0.5] + [0.0] * 1534, importance=1.0, age_days=0)
    order = [m.id for m in await repo.search(
        tenant_id=TEST_TENANT, caller_type="agent", caller_id="a", query_vector=q, top_k=2,
        type_filter=None, tags_filter=None, include_shared=True, user_scope_visibility="isolated",
    )]
    # With scoring OFF (default) the high-importance/recent 'b' does NOT jump 'a'.
    assert order[0] == a and order[1] == b
