"""B4 — ACT-R base-level activation (recency x frequency, power-law decay).

Covers the pure math, the composite integration behind decay='power_actr', the read-path
access_count bump, and the frequency tie-break (a high-frequency memory overtakes a stale
one-off at equal cosine/recency/importance) — while the default exponential path is unchanged.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from _helpers import TEST_TENANT
from memory_service.services import scoring
from memory_service.services.repository import InMemoryRepository, new_memory
from memory_service.services.scoring import (
    ScoringWeights,
    actr_recency,
    base_level_activation,
    composite_score,
)


def test_base_level_activation_matches_petrov_formula() -> None:
    # B = ln(n) - d*ln(age) at frequency_weight=1.0.
    assert base_level_activation(1, 1.0, 0.5) == pytest.approx(0.0)
    assert base_level_activation(10, 1.0, 0.5) == pytest.approx(math.log(10))
    assert base_level_activation(1, 100.0, 0.5) == pytest.approx(-0.5 * math.log(100))
    # count / age are floored so the logs stay finite.
    assert base_level_activation(0, 0.0, 0.5) == pytest.approx(0.0)
    # frequency_weight scales the ln(n) term (0 => pure power-law recency).
    assert base_level_activation(50, 10.0, 0.5, frequency_weight=0.0) == pytest.approx(
        -0.5 * math.log(10)
    )


def test_actr_recency_monotonic_in_frequency_and_age() -> None:
    # More retrievals at equal age -> higher; older at equal frequency -> lower. Bounded [0,1].
    hot = actr_recency(100, 3600.0, 0.5)
    cold = actr_recency(1, 3600.0, 0.5)
    stale = actr_recency(1, 3600.0 * 24 * 365, 0.5)
    assert 0.0 <= stale <= cold <= hot <= 1.0
    assert hot > cold > stale


def test_composite_power_actr_rewards_frequency_at_equal_everything_else() -> None:
    now = datetime.now(UTC)
    ref = now - timedelta(days=5)
    w = ScoringWeights(decay="power_actr", frequency_weight=1.0, actr_decay_d=0.5)
    hot = composite_score(cosine=0.5, importance=0.5, reference=ref, now=now, weights=w,
                          access_count=90)
    cold = composite_score(cosine=0.5, importance=0.5, reference=ref, now=now, weights=w,
                           access_count=1)
    assert hot > cold


def test_default_exponential_ignores_access_count() -> None:
    now = datetime.now(UTC)
    ref = now - timedelta(days=5)
    w = ScoringWeights()  # decay='exponential' by default
    a = composite_score(cosine=0.5, importance=0.5, reference=ref, now=now, weights=w,
                        access_count=90)
    b = composite_score(cosine=0.5, importance=0.5, reference=ref, now=now, weights=w,
                        access_count=1)
    assert a == b  # frequency is inert unless decay='power_actr'


def _mk(repo: InMemoryRepository, mid: str, content: str, vector: list[float], *,
        access_count: int, age_days: float) -> None:
    mem = new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="fact", tags=[], content=content, metadata={}, vector=vector, session_id=None,
        ttl_seconds=None, importance_score=0.5,
    )
    mem.id = mid
    created = datetime.now(UTC) - timedelta(days=age_days)
    mem.created_at = created
    mem.last_accessed_at = created
    mem.last_retrieved_at = created
    mem.access_count = access_count
    repo._memories[mid] = mem  # noqa: SLF001 — test seed


@pytest.mark.asyncio
async def test_read_path_bumps_access_count() -> None:
    repo = InMemoryRepository()
    _mk(repo, "m1", "alpha beta", [1.0, 0.0] + [0.0] * 1534, access_count=3, age_days=1)
    before = repo._memories["m1"].access_count  # noqa: SLF001
    await repo.search(
        tenant_id=TEST_TENANT, caller_type="agent", caller_id="a",
        query_vector=[1.0, 0.0] + [0.0] * 1534, top_k=1, type_filter=None, tags_filter=None,
        include_shared=True, user_scope_visibility="isolated",
    )
    assert repo._memories["m1"].access_count == before + 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_frequency_breaks_a_cosine_recency_tie_under_power_actr() -> None:
    q = [1.0, 0.0] + [0.0] * 1534
    common = {
        "tenant_id": TEST_TENANT, "caller_type": "agent", "caller_id": "a", "query_vector": q,
        "top_k": 1, "type_filter": None, "tags_filter": None, "include_shared": True,
        "user_scope_visibility": "isolated",
    }

    def _fresh() -> InMemoryRepository:
        # A FRESH repo per mode: a search bumps last_retrieved_at/access_count, so reusing one
        # repo would let the first search's timestamp bump contaminate the second.
        r = InMemoryRepository()
        # Identical vectors + age + importance -> only access_count differs.
        _mk(r, "rare", "same text", q, access_count=1, age_days=5)   # inserted first
        _mk(r, "hot", "same text", q, access_count=90, age_days=5)
        return r

    cosine = await _fresh().search(**common)
    assert cosine[0].id == "rare"  # tie -> stable input order keeps the rarely-used one
    actr = await _fresh().search(
        **common, scoring_enabled=True,
        scoring_weights=ScoringWeights(decay="power_actr", frequency_weight=1.0, actr_decay_d=0.5),
    )
    assert actr[0].id == "hot"  # frequency reinforcement promotes the oft-retrieved memory


def test_weights_from_settings_carries_actr_knobs() -> None:
    class S:
        memory_scoring_decay = "power_actr"
        memory_scoring_frequency_weight = 0.7
        memory_actr_decay_d = 0.4

    w = scoring.weights_from_settings(S())
    assert w.decay == "power_actr"
    assert w.frequency_weight == 0.7
    assert w.actr_decay_d == 0.4
