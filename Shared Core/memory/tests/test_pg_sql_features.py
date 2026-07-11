"""PG-repo SQL emission for B1 (halfvec), B3 (ef_search), B6 (MMR fetch), B7 (links).

These build their SQL dynamically from config, so neither the InMemoryRepository (no SQL) nor
``inspect.getsource`` (pre-interpolation) can verify them. The RecordingConn fake captures the
exact statements the repo sends. The fake DB swallows the ``hnsw.ef_search`` GUC — so this
proves the SQL is EMITTED, not that ANN recall changes (that needs a live pgvector track).
"""

from __future__ import annotations

import pytest

from _pg_fakes import RecordingConn, RecordingPool, full_row
from memory_service.services import repository
from memory_service.services.pg_repository import PgMemoryRepository

_TENANT = "00000000-0000-0000-0000-0000000000aa"
_SEARCH = {
    "tenant_id": _TENANT, "caller_type": "agent", "caller_id": "agent-aaaa",
    "query_vector": [0.1, 0.2, 0.3, 0.4], "top_k": 5, "type_filter": None, "tags_filter": None,
    "include_shared": True, "user_scope_visibility": "isolated",
}


def _repo(conn: RecordingConn, **kw) -> PgMemoryRepository:  # type: ignore[no-untyped-def]
    return PgMemoryRepository(
        RecordingPool(conn), producer_version="1.0.0", default_visibility="isolated", **kw
    )


def _mem() -> repository.StoredMemory:
    return repository.new_memory(
        tenant_id=_TENANT, principal_type="agent", principal_id="agent-aaaa", scope="principal_only",
        type="note", tags=[], content="hello world", metadata={}, vector=[0.1, 0.2, 0.3, 0.4],
        session_id=None, ttl_seconds=None,
    )


# ── B1: halfvec / binary_rerank casts ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_b1_default_off_uses_full_vector_no_halfvec() -> None:
    conn = RecordingConn(cursor_rows=[[]])
    await _repo(conn).search(**_SEARCH)
    assert "%(qvec)s::vector" in conn.sql
    assert "halfvec" not in conn.sql


@pytest.mark.asyncio
async def test_b1_halfvec_casts_ann_scan() -> None:
    conn = RecordingConn(cursor_rows=[[]])
    await _repo(conn, vector_quantization="halfvec").search(**_SEARCH)
    assert "::halfvec(1536)" in conn.sql


@pytest.mark.asyncio
async def test_b1_binary_rerank_uses_bit_hamming_first_pass() -> None:
    conn = RecordingConn(cursor_rows=[[]])
    await _repo(conn, vector_quantization="binary_rerank").search(**_SEARCH)
    assert "binary_quantize" in conn.sql and "::bit(1536)" in conn.sql
    # The candidate distance surfaced for rerank/similarity stays full-precision cosine.
    assert "v.embedding <=> %(qvec)s::vector" in conn.sql


@pytest.mark.asyncio
async def test_b1_halfvec_casts_store_dedup_scan() -> None:
    conn = RecordingConn(cursor_rows=[[]])
    await _repo(conn, vector_quantization="halfvec").store(
        memory=_mem(), dedup_threshold=0.95, trace_id="t", producer_version="1.0.0"
    )
    assert "::halfvec(1536)" in conn.sql


# ── B3: query-time ef_search GUC ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_b3_ef_search_emitted_in_search_and_store_when_set() -> None:
    csearch = RecordingConn(cursor_rows=[[]])
    await _repo(csearch, hnsw_ef_search=200).search(**_SEARCH)
    assert "SET LOCAL hnsw.ef_search = 200" in csearch.sql

    cstore = RecordingConn(cursor_rows=[[]])
    await _repo(cstore, hnsw_ef_search=200).store(
        memory=_mem(), dedup_threshold=0.95, trace_id="t", producer_version="1.0.0"
    )
    assert "SET LOCAL hnsw.ef_search = 200" in cstore.sql


@pytest.mark.asyncio
async def test_b3_ef_search_not_emitted_by_default() -> None:
    conn = RecordingConn(cursor_rows=[[]])
    await _repo(conn).search(**_SEARCH)  # hnsw_ef_search defaults to 0
    assert "hnsw.ef_search" not in conn.sql


# ── B6: candidate embedding fetched ONLY when MMR is on ───────────────────────────────
@pytest.mark.asyncio
async def test_b6_embedding_fetched_only_when_mmr_enabled() -> None:
    off = RecordingConn(cursor_rows=[[]])
    await _repo(off).search(**_SEARCH)
    assert "v.embedding::text AS embedding" not in off.sql

    on = RecordingConn(cursor_rows=[[]])
    await _repo(on).search(**_SEARCH, mmr_enabled=True)
    assert "v.embedding::text AS embedding" in on.sql


# ── B7: link edge write at ingest + 1-hop expansion at retrieval ──────────────────────
@pytest.mark.asyncio
async def test_b7_store_writes_link_edges_for_related_neighbour() -> None:
    # Neighbour cosine 0.7: below dedup (0.95) but above sim_min (0.5) => an edge is written.
    neighbour = {"id": "22222222-2222-2222-2222-222222222222", "content": "related", "similarity": 0.7}
    conn = RecordingConn(cursor_rows=[[neighbour]])
    await _repo(conn).store(
        memory=_mem(), dedup_threshold=0.95, trace_id="t", producer_version="1.0.0",
        linking_enabled=True, linking_sim_min=0.5, linking_max_neighbors=3,
    )
    assert "INSERT INTO memory.memory_links" in conn.sql


@pytest.mark.asyncio
async def test_b7_store_writes_no_edge_when_no_related_neighbour() -> None:
    # Neighbour cosine 0.2: below sim_min => associative, not related enough => no edge.
    neighbour = {"id": "22222222-2222-2222-2222-222222222222", "content": "unrelated", "similarity": 0.2}
    conn = RecordingConn(cursor_rows=[[neighbour]])
    await _repo(conn).store(
        memory=_mem(), dedup_threshold=0.95, trace_id="t", producer_version="1.0.0",
        linking_enabled=True, linking_sim_min=0.5, linking_max_neighbors=3,
    )
    assert "INSERT INTO memory.memory_links" not in conn.sql


@pytest.mark.asyncio
async def test_b7_search_expands_links_and_appends_linked_row() -> None:
    cand = full_row(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", content="seed")
    linked = full_row(id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", content="linked only")
    conn = RecordingConn(cursor_rows=[[cand], [linked]])  # main query, then expansion query
    res = await _repo(conn).search(**_SEARCH, linking_enabled=True)
    assert "FROM memory.memory_links l" in conn.sql
    ids = [m.id for m in res]
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in ids
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in ids  # link-only memory surfaced


@pytest.mark.asyncio
async def test_b7_search_no_expansion_query_when_disabled() -> None:
    cand = full_row(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    conn = RecordingConn(cursor_rows=[[cand]])
    await _repo(conn).search(**_SEARCH)  # linking off by default
    assert "memory.memory_links" not in conn.sql
