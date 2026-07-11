"""B7 — associative linking + 1-hop graph expansion: decide_links, in-memory write+expand,
the anti-leak guard on expansion, flag-off no-op, and API wiring.
"""

from __future__ import annotations

import pytest

from _helpers import AGENT_A, AGENT_B, TEST_TENANT, bind_principal, make_principal
from memory_service.services.linking import LinkCandidate, decide_links
from memory_service.services.repository import InMemoryRepository, new_memory


def test_decide_links_keeps_associated_but_not_duplicate_neighbours() -> None:
    cands = [
        LinkCandidate("a", 0.70),   # associated -> keep
        LinkCandidate("b", 0.90),   # associated -> keep
        LinkCandidate("c", 0.30),   # below sim_min -> drop
        LinkCandidate("d", 0.96),   # >= dedup_threshold (a near-dup) -> drop
    ]
    out = decide_links(cands, sim_min=0.5, dedup_threshold=0.95, max_neighbors=3)
    assert [d.dst_memory_id for d in out] == ["b", "a"]  # highest similarity first
    assert all(d.relation == "associated" for d in out)
    assert out[0].weight == 0.90


@pytest.mark.asyncio
async def test_store_writes_edges_and_search_expands_one_hop() -> None:
    repo = InMemoryRepository()
    q = [1.0, 0.0, 0.0] + [0.0] * 1533
    # A matches the query; B is associated to A (cosine 0.6) but far from the query.
    a = [1.0, 0.0, 0.0] + [0.0] * 1533
    b = [0.6, 0.8, 0.0] + [0.0] * 1533
    await repo.store(memory=_mem("A", a), dedup_threshold=0.95, trace_id="t", producer_version="1",
                     linking_enabled=True, linking_sim_min=0.5, linking_max_neighbors=3)
    await repo.store(memory=_mem("B", b), dedup_threshold=0.95, trace_id="t", producer_version="1",
                     linking_enabled=True, linking_sim_min=0.5, linking_max_neighbors=3)
    assert "A" in repo._links and any(dst == "B" for dst, _, _ in repo._links["A"])  # noqa: SLF001

    common = {
        "tenant_id": TEST_TENANT, "caller_type": "agent", "caller_id": "a", "query_vector": q,
        "top_k": 1, "type_filter": None, "tags_filter": None, "include_shared": True,
        "user_scope_visibility": "isolated",
    }
    vector_only = [m.id for m in await repo.search(**common)]
    assert vector_only == ["A"]  # single-shot cosine misses B
    expanded = [m.id for m in await repo.search(**common, linking_enabled=True)]
    assert "A" in expanded and "B" in expanded  # 1-hop expansion surfaces B


@pytest.mark.asyncio
async def test_expansion_never_leaks_across_principals() -> None:
    repo = InMemoryRepository()
    a = [1.0, 0.0, 0.0] + [0.0] * 1533
    b = [0.6, 0.8, 0.0] + [0.0] * 1533
    # Both memories belong to AGENT_B and are linked.
    await repo.store(memory=_mem("A", a, principal_id=AGENT_B), dedup_threshold=0.95,
                     trace_id="t", producer_version="1", linking_enabled=True,
                     linking_sim_min=0.5, linking_max_neighbors=3)
    await repo.store(memory=_mem("B", b, principal_id=AGENT_B), dedup_threshold=0.95,
                     trace_id="t", producer_version="1", linking_enabled=True,
                     linking_sim_min=0.5, linking_max_neighbors=3)
    # AGENT_A searches WITH expansion on: must see nothing (principal_only never crosses).
    got = await repo.search(
        tenant_id=TEST_TENANT, caller_type="agent", caller_id=AGENT_A,
        query_vector=[1.0, 0.0, 0.0] + [0.0] * 1533, top_k=5, type_filter=None, tags_filter=None,
        include_shared=True, user_scope_visibility="isolated", linking_enabled=True,
    )
    assert got == []


@pytest.mark.asyncio
async def test_flag_off_writes_no_edges() -> None:
    repo = InMemoryRepository()
    a = [1.0, 0.0, 0.0] + [0.0] * 1533
    b = [0.6, 0.8, 0.0] + [0.0] * 1533
    await repo.store(memory=_mem("A", a), dedup_threshold=0.95, trace_id="t", producer_version="1")
    await repo.store(memory=_mem("B", b), dedup_threshold=0.95, trace_id="t", producer_version="1")
    assert repo._links == {}  # noqa: SLF001 — default off => no edges


@pytest.mark.asyncio
async def test_linking_wired_through_api_expands_results(app_client) -> None:  # type: ignore[no-untyped-def]
    app, ac = app_client
    bind_principal(app, make_principal())
    # sim_min=-1.0 makes any distinct pair associate (the SHA mock embeds are ~orthogonal).
    app.state.settings.memory_linking_enabled = True
    app.state.settings.memory_linking_sim_min = -1.0
    await ac.post("/v1/memories", json={"content": "project apollo is led by alice"})
    await ac.post("/v1/memories", json={"content": "alice email is alice at corp"})
    s = await ac.post("/v1/memories/search", json={"query": "apollo", "top_k": 1})
    assert s.status_code == 200
    assert s.json()["count"] == 2  # top_k=1 vector hit + one linked memory appended


def _mem(mid: str, vector: list[float], *, principal_id: str = "a"):  # type: ignore[no-untyped-def]
    m = new_memory(
        tenant_id=TEST_TENANT, principal_type="agent", principal_id=principal_id,
        scope="principal_only", type="note", tags=[], content=mid, metadata={}, vector=vector,
        session_id=None, ttl_seconds=None,
    )
    m.id = mid
    return m
