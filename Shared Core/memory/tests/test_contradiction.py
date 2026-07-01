"""Contradiction / temporal validity (supersession) — pure-function + repo behavior.

OFF by default: with the flag off a conflicting store inserts a fresh row exactly as today
and search returns everything. ON: the prior conflicting memory is marked superseded (kept,
not deleted) and current-only search hides it. The principal anti-leak rule is untouched.
"""

from __future__ import annotations

import pytest

from _helpers import TEST_TENANT
from memory_service.services import contradiction
from memory_service.services.repository import InMemoryRepository, new_memory


# ── Pure predicate ─────────────────────────────────────────────────────────────────────
def test_is_contradiction_same_subject_changed_value() -> None:
    assert contradiction.is_contradiction(
        new_content="My favorite color is red now",
        prior_content="My favorite color is blue",
        cosine_similarity=0.9, sim_min=0.8, dedup_threshold=0.95,
    )


def test_is_contradiction_skips_unrelated() -> None:
    assert not contradiction.is_contradiction(
        new_content="The sky is blue", prior_content="I like pizza",
        cosine_similarity=0.85, sim_min=0.8, dedup_threshold=0.95,
    )


def test_is_contradiction_skips_exact_duplicate() -> None:
    # Identical -> dedup territory, never a contradiction.
    assert not contradiction.is_contradiction(
        new_content="My favorite color is blue", prior_content="My favorite color is blue",
        cosine_similarity=0.99, sim_min=0.8, dedup_threshold=0.95,
    )


def test_is_contradiction_skips_below_sim_min_and_above_dedup() -> None:
    # Below sim_min: not about the same thing.
    assert not contradiction.is_contradiction(
        new_content="My color is red", prior_content="My color is blue",
        cosine_similarity=0.5, sim_min=0.8, dedup_threshold=0.95,
    )
    # At/above dedup: the dedup path owns it, not contradiction.
    assert not contradiction.is_contradiction(
        new_content="My color is red", prior_content="My color is blue",
        cosine_similarity=0.96, sim_min=0.8, dedup_threshold=0.95,
    )


def test_jaccard_overlap() -> None:
    assert contradiction.jaccard_overlap("the user lives in berlin", "the user lives in paris") > 0.4
    assert contradiction.jaccard_overlap("apples", "oranges") == 0.0


# ── Repo behavior ──────────────────────────────────────────────────────────────────────
def _mk(content: str, vector: list[float]):  # type: ignore[no-untyped-def]
    return new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="fact", tags=[], content=content, metadata={}, vector=vector, session_id=None,
        ttl_seconds=None,
    )


_V1 = [1.0, 0.0, 0.0] + [0.0] * 1533
_V2 = [0.9, 0.43, 0.0] + [0.0] * 1533  # cosine ~0.9 with _V1 (>= sim_min, < dedup)


@pytest.mark.asyncio
async def test_supersession_marks_prior_when_enabled() -> None:
    repo = InMemoryRepository(contradiction_enabled=True, contradiction_sim_min=0.8)
    r1 = await repo.store(memory=_mk("My favorite color is blue", _V1),
                          dedup_threshold=0.95, trace_id="t", producer_version="0")
    r2 = await repo.store(memory=_mk("My favorite color is red now", _V2),
                          dedup_threshold=0.95, trace_id="t", producer_version="0")
    assert r1.memory.valid_until is not None
    assert r1.memory.superseded_by_id == r2.memory.id
    assert any(a["action"] == "superseded" for a in repo.audit)

    common = {
        "tenant_id": TEST_TENANT, "caller_type": "agent", "caller_id": "a", "query_vector": _V1,
        "top_k": 10, "type_filter": None, "tags_filter": None, "include_shared": True,
        "user_scope_visibility": "isolated",
    }
    current = [m.content for m in await repo.search(**common, current_only=True)]
    assert current == ["My favorite color is red now"]
    # The superseded memory is preserved and still retrievable when asked for.
    allmem = [m.content for m in await repo.search(**common, current_only=False)]
    assert "My favorite color is blue" in allmem


@pytest.mark.asyncio
async def test_no_supersession_when_disabled_default() -> None:
    repo = InMemoryRepository()  # contradiction OFF (default)
    r1 = await repo.store(memory=_mk("My favorite color is blue", _V1),
                          dedup_threshold=0.95, trace_id="t", producer_version="0")
    await repo.store(memory=_mk("My favorite color is red now", _V2),
                     dedup_threshold=0.95, trace_id="t", producer_version="0")
    # Default: nothing superseded; both rows present; audit empty.
    assert r1.memory.valid_until is None
    assert r1.memory.superseded_by_id is None
    assert repo.audit == []
    res = await repo.search(
        tenant_id=TEST_TENANT, caller_type="agent", caller_id="a", query_vector=_V1, top_k=10,
        type_filter=None, tags_filter=None, include_shared=True, user_scope_visibility="isolated",
        current_only=True,  # even current_only shows both (neither is superseded)
    )
    assert len(res) == 2


@pytest.mark.asyncio
async def test_exact_duplicate_still_dedups_not_supersedes() -> None:
    repo = InMemoryRepository(contradiction_enabled=True, contradiction_sim_min=0.8)
    r1 = await repo.store(memory=_mk("same content here", _V1),
                          dedup_threshold=0.95, trace_id="t", producer_version="0")
    r2 = await repo.store(memory=_mk("same content here", _V1),
                          dedup_threshold=0.95, trace_id="t", producer_version="0")
    assert r2.deduped is True
    assert r2.memory.id == r1.memory.id
    assert r1.memory.valid_until is None  # dedup, not supersession
    assert repo.audit == []
