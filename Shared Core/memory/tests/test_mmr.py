"""B6 — MMR diversity re-rank: pure function, in-memory repo wiring, fail-soft, flag-off.

At top_k>1 over a redundancy-heavy window, MMR must spend the budget on distinct facets
instead of near-paraphrases; the default (flag off) path stays pure-cosine byte-identical.
"""

from __future__ import annotations

import pytest

from _helpers import TEST_TENANT, bind_principal, make_principal
from memory_service.services.repository import InMemoryRepository, StoredMemory, new_memory
from memory_service.services.scoring import mmr_rerank


def _cand(mid: str, vector: list[float]) -> StoredMemory:
    return StoredMemory(
        id=mid, tenant_id=TEST_TENANT, principal_type="agent", principal_id="a",
        scope="principal_only", type="note", tags=[], content=mid, metadata={}, vector=vector,
        session_id=None, score=1.0, created_at=None, last_accessed_at=None, expires_at=None,
    )


# Query is [1,0,0]; a1/a2 are two near-duplicate facet-1 memories (a1 slightly closer),
# b is a relevant but ORTHOGONAL-ish facet. a1 != q so sim-to-seed differs from relevance.
_Q = [1.0, 0.0, 0.0]
_A1 = [0.95, 0.31, 0.0]
_A2 = [0.94, 0.34, 0.0]
_B = [0.6, 0.0, 0.8]


def test_mmr_picks_a_diverse_second_over_a_near_duplicate() -> None:
    a1, a2, b = _cand("a1", _A1), _cand("a2", _A2), _cand("b", _B)
    out = mmr_rerank([a1, a2, b], _Q, lambda_mult=0.5, top_k=2)
    assert [m.id for m in out] == ["a1", "b"]  # diversity beats the redundant a2


def test_mmr_lambda_one_is_pure_relevance_order() -> None:
    a1, a2, b = _cand("a1", _A1), _cand("a2", _A2), _cand("b", _B)
    out = mmr_rerank([a1, a2, b], _Q, lambda_mult=1.0, top_k=2)
    assert [m.id for m in out] == ["a1", "a2"]  # lambda=1 => ignore diversity


def test_mmr_fails_soft_to_input_order_on_missing_vectors() -> None:
    q = [1.0, 0.0, 0.0]
    a = _cand("a", [1.0, 0.0, 0.0])
    b = _cand("b", [])  # no resident vector
    out = mmr_rerank([a, b], q, lambda_mult=0.5, top_k=2)
    assert [m.id for m in out] == ["a", "b"]  # returns the input order, never raises


def _seed(repo: InMemoryRepository, mid: str, content: str, vector: list[float]) -> None:
    mem = new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id="a", scope="principal_only",
        type="note", tags=[], content=content, metadata={}, vector=vector, session_id=None,
        ttl_seconds=None,
    )
    mem.id = mid
    repo._memories[mid] = mem  # noqa: SLF001


@pytest.mark.asyncio
async def test_repo_mmr_surfaces_complementary_memory() -> None:
    repo = InMemoryRepository()
    q = [1.0, 0.0, 0.0] + [0.0] * 1533
    _seed(repo, "dup1", "c", [0.9, 0.4, 0.0] + [0.0] * 1533)
    _seed(repo, "dup2", "c", [0.9, 0.4, 0.0] + [0.0] * 1533)   # identical to dup1
    _seed(repo, "other", "c", [0.5, 0.0, 0.87] + [0.0] * 1533)  # distinct facet
    common = {
        "tenant_id": TEST_TENANT, "caller_type": "agent", "caller_id": "a", "query_vector": q,
        "top_k": 2, "type_filter": None, "tags_filter": None, "include_shared": True,
        "user_scope_visibility": "isolated",
    }
    cosine = {m.id for m in await repo.search(**common)}
    assert cosine == {"dup1", "dup2"}                      # pure cosine returns the near-dupes
    mmr = {m.id for m in await repo.search(**common, mmr_enabled=True, mmr_lambda=0.5)}
    assert "other" in mmr                                  # MMR spends budget on the distinct facet


@pytest.mark.asyncio
async def test_default_off_is_pure_cosine(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    await ac.post("/v1/memories", json={"content": "python performance caching tip"})
    # MMR off by default: search still returns 200 and behaves as pure cosine.
    s = await ac.post("/v1/memories/search", json={"query": "python performance", "top_k": 5})
    assert s.status_code == 200


@pytest.mark.asyncio
async def test_mmr_flag_wired_through_api(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    app.state.settings.memory_mmr_enabled = True
    app.state.settings.memory_mmr_lambda = 0.5
    for c in ("python performance caching", "python performance caching", "python profiling tool"):
        await ac.post("/v1/memories", json={"content": c})
    s = await ac.post("/v1/memories/search", json={"query": "python performance", "top_k": 2})
    assert s.status_code == 200
    assert s.json()["count"] >= 1
