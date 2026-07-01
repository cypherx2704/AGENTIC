"""Hybrid (dense+lexical RRF) / sparse search, rerank client, and contextual ingest.

These exercise the new ADDITIVE retrieval upgrades directly against the in-memory FakeDb +
FakePool (the same in-process style as test_worker.py), so they need no live stack / lifespan.
The default-behaviour invariants (dense path unchanged, flags default off) are asserted too.
"""

from __future__ import annotations

import httpx
import pytest

from rag_service.core.config import Settings
from rag_service.services.contextual import Contextualizer
from rag_service.services.embeddings import mock_embed
from rag_service.services.ingest import ingest_text
from rag_service.services.rerank import RerankClient, mock_rerank
from rag_service.services.store.base import ChunkVector
from rag_service.services.store.pgvector import PgVectorAdapter

from .conftest import TEST_TENANT
from .fakes import FakeDb, FakePool

TENANT = TEST_TENANT
KB = "kb-hybrid"
DIM = 1536


def _seed_chunk(db: FakeDb, *, chunk_id: str, doc_id: str, content: str, context: str = "") -> None:
    """Insert a chunk + its mock embedding directly into the fake store."""
    meta: dict = {"doc_name": "d.md", "source_uri": "s3://b/x"}
    if context:
        meta["context"] = context
    db.chunks.append({
        "chunk_id": chunk_id, "doc_id": doc_id, "kb_id": KB, "tenant_id": TENANT,
        "content": content, "chunk_index": 0, "embedding_model": "m", "embedding_dim": DIM,
        "metadata": meta, "created_at": None,
    })
    # Embed the (context + content) just like the real ingest path would.
    embed_text = f"{context}\n\n{content}" if context else content
    db.chunk_vectors_1536.append({
        "chunk_id": chunk_id, "tenant_id": TENANT, "kb_id": KB,
        "embedding": mock_embed([embed_text], DIM)[0],
    })


def _adapter() -> tuple[PgVectorAdapter, FakeDb]:
    db = FakeDb()
    settings = Settings(mock_embeddings=True)
    return PgVectorAdapter(FakePool(db), settings), db


# ── Hybrid / sparse search ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hybrid_fuses_dense_and_lexical() -> None:
    adapter, db = _adapter()
    _seed_chunk(db, chunk_id="c1", doc_id="d1",
                content="The refund policy allows a full refund within 30 days for enterprise.")
    _seed_chunk(db, chunk_id="c2", doc_id="d2",
                content="Shipping is free for orders over fifty dollars worldwide.")
    _seed_chunk(db, chunk_id="c3", doc_id="d3",
                content="Refund requests for refund eligibility require an account in good standing.")

    query = "refund policy"
    qvec = mock_embed([query], DIM)[0]
    hits = await adapter.search_hybrid(
        TENANT, KB, qvec, query,
        top_k=3, candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    ids = [h.chunk_id for h in hits]
    # Lexically-matching refund chunks rank above the unrelated shipping chunk.
    assert "c1" in ids and "c3" in ids
    assert ids.index("c1") < ids.index("c2")
    # Fused scores are positive RRF scores (not cosine), descending.
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(s > 0 for s in scores)


@pytest.mark.asyncio
async def test_sparse_only_uses_lexical_leg() -> None:
    adapter, db = _adapter()
    _seed_chunk(db, chunk_id="c1", doc_id="d1", content="alpha beta gamma refund")
    _seed_chunk(db, chunk_id="c2", doc_id="d2", content="completely unrelated content here")
    # Sparse passes embedding=None — only lexical-matching chunks come back.
    hits = await adapter.search_hybrid(
        TENANT, KB, None, "refund",
        top_k=5, candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="sparse",
    )
    ids = [h.chunk_id for h in hits]
    assert ids == ["c1"]  # only the lexical match


@pytest.mark.asyncio
async def test_hybrid_respects_metadata_filter() -> None:
    adapter, db = _adapter()
    db.chunks.append({
        "chunk_id": "c1", "doc_id": "d1", "kb_id": KB, "tenant_id": TENANT,
        "content": "refund policy details", "chunk_index": 0, "embedding_model": "m",
        "embedding_dim": DIM, "metadata": {"lang": "en"}, "created_at": None,
    })
    db.chunk_vectors_1536.append({
        "chunk_id": "c1", "tenant_id": TENANT, "kb_id": KB,
        "embedding": mock_embed(["refund policy details"], DIM)[0],
    })
    db.chunks.append({
        "chunk_id": "c2", "doc_id": "d2", "kb_id": KB, "tenant_id": TENANT,
        "content": "refund policy other", "chunk_index": 0, "embedding_model": "m",
        "embedding_dim": DIM, "metadata": {"lang": "fr"}, "created_at": None,
    })
    db.chunk_vectors_1536.append({
        "chunk_id": "c2", "tenant_id": TENANT, "kb_id": KB,
        "embedding": mock_embed(["refund policy other"], DIM)[0],
    })
    hits = await adapter.search_hybrid(
        TENANT, KB, mock_embed(["refund"], DIM)[0], "refund policy",
        top_k=5, candidates=50, rrf_k=60, filters={"lang": "en"}, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    assert [h.chunk_id for h in hits] == ["c1"]


@pytest.mark.asyncio
async def test_hybrid_rls_isolated_by_tenant() -> None:
    adapter, db = _adapter()
    _seed_chunk(db, chunk_id="c1", doc_id="d1", content="refund policy here")
    # A chunk owned by another tenant must never surface (RLS via the fake's tenant filter).
    other = "00000000-0000-0000-0000-0000000000cc"
    db.chunks.append({
        "chunk_id": "c2", "doc_id": "d2", "kb_id": KB, "tenant_id": other,
        "content": "refund policy secret", "chunk_index": 0, "embedding_model": "m",
        "embedding_dim": DIM, "metadata": {}, "created_at": None,
    })
    db.chunk_vectors_1536.append({
        "chunk_id": "c2", "tenant_id": other, "kb_id": KB,
        "embedding": mock_embed(["refund policy secret"], DIM)[0],
    })
    hits = await adapter.search_hybrid(
        TENANT, KB, mock_embed(["refund"], DIM)[0], "refund policy",
        top_k=5, candidates=50, rrf_k=60, filters=None, dimension=DIM, ef_search=100,
        mode="hybrid",
    )
    assert [h.chunk_id for h in hits] == ["c1"]


@pytest.mark.asyncio
async def test_dense_search_unchanged_default_path() -> None:
    """The existing two-pass dense `search` still returns cosine scores in [0,1]."""
    adapter, db = _adapter()
    content = "The refund policy allows a full refund within 30 days."
    _seed_chunk(db, chunk_id="c1", doc_id="d1", content=content)
    hits = await adapter.search(
        TENANT, KB, mock_embed([content], DIM)[0],
        top_k=3, min_score=0.5, filters=None, dimension=DIM, ef_search=100,
    )
    assert len(hits) == 1
    assert hits[0].chunk_id == "c1"
    assert hits[0].score >= 0.99  # exact-vector match (cosine)


# ── Rerank client ───────────────────────────────────────────────────────────────
def test_mock_rerank_orders_by_query_overlap() -> None:
    docs = ["totally unrelated text", "the refund policy is generous", "refund refund refund policy"]
    items = mock_rerank("refund policy", docs, top_n=3)
    # Both refund docs (idx 1, 2) match both query tokens -> rank above the unrelated doc (idx 0).
    order = [it.index for it in items]
    assert order[-1] == 0  # the unrelated doc is last
    assert set(order[:2]) == {1, 2}
    assert items[-1].relevance_score < items[0].relevance_score


@pytest.mark.asyncio
async def test_rerank_mock_mode() -> None:
    settings = Settings(mock_embeddings=True, rag_rerank_enabled=True)
    client = RerankClient(settings)
    res = await client.rerank("refund", ["a refund here", "no match"], top_n=2)
    assert res.source == "mock"
    assert res.items[0].index == 0


@pytest.mark.asyncio
async def test_rerank_via_llms_parses_results() -> None:
    settings = Settings(mock_embeddings=False, mock_rerank=False, rag_rerank_enabled=True)

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "rerank",
            "results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.4},
            ],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(_ok))

    class _FakeTokens:
        async def get_token(self, *, on_behalf_of=None):  # noqa: ANN001
            return "svc.jwt"

    client = RerankClient(settings, token_provider=_FakeTokens(), client=http)
    res = await client.rerank("q", ["d0", "d1", "d2"], top_n=2, agent_jwt="a.jwt")
    assert res.source == "llms"
    assert [it.index for it in res.items] == [2, 0]


@pytest.mark.asyncio
async def test_rerank_falls_back_to_base_on_failure() -> None:
    settings = Settings(
        mock_embeddings=False, mock_rerank=False,
        rag_rerank_enabled=True, rerank_fallback_to_base=True,
    )

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("rerank down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(_boom))

    class _FakeTokens:
        async def get_token(self, *, on_behalf_of=None):  # noqa: ANN001
            return "svc.jwt"

    client = RerankClient(settings, token_provider=_FakeTokens(), client=http)
    res = await client.rerank("q", ["d0", "d1"], top_n=2, agent_jwt="a.jwt")
    assert res.source == "fallback_base"
    # Base ordering preserved (indices in original order).
    assert [it.index for it in res.items] == [0, 1]


# ── Contextual ingest ─────────────────────────────────────────────────────────────
class _StubStore:
    """Captures the ChunkVectors that ingest_text would upsert."""

    def __init__(self) -> None:
        self.vectors: list[ChunkVector] = []

    async def upsert(self, tenant_id: str, chunks: list[ChunkVector]) -> int:
        self.vectors.extend(chunks)
        return len(chunks)


@pytest.mark.asyncio
async def test_contextual_ingest_off_by_default_no_context_metadata() -> None:
    from rag_service.services.embeddings import EmbeddingClient

    settings = Settings(mock_embeddings=True)  # rag_contextual_ingest defaults False
    store = _StubStore()
    ctxr = Contextualizer(settings)
    await ingest_text(
        text="The refund policy is generous. Standard plans differ.",
        doc_id="d1", kb_id=KB, tenant_id=TENANT,
        embedding_model="m", embedding_dim=DIM,
        chunking_strategy="sentence", chunk_size=512, chunk_overlap=50,
        doc_name="d.md", source_uri=None,
        embedder=EmbeddingClient(settings), store=store, settings=settings,
        contextualizer=ctxr,
    )
    assert store.vectors
    assert all("context" not in v.metadata for v in store.vectors)


@pytest.mark.asyncio
async def test_contextual_ingest_on_adds_context_metadata() -> None:
    from rag_service.services.embeddings import EmbeddingClient

    settings = Settings(mock_embeddings=True, rag_contextual_ingest=True)
    store = _StubStore()
    ctxr = Contextualizer(settings)
    await ingest_text(
        text="Acme Refund Handbook\nThe refund policy is generous. Standard plans differ.",
        doc_id="d1", kb_id=KB, tenant_id=TENANT,
        embedding_model="m", embedding_dim=DIM,
        chunking_strategy="sentence", chunk_size=512, chunk_overlap=50,
        doc_name="d.md", source_uri=None,
        embedder=EmbeddingClient(settings), store=store, settings=settings,
        contextualizer=ctxr,
    )
    assert store.vectors
    assert all(v.metadata.get("context") for v in store.vectors)
    # The embedding now reflects (context + content): differs from the no-context vector.
    plain = mock_embed([store.vectors[0].content], DIM)[0]
    assert store.vectors[0].embedding != plain


# ── App-level query integration (hybrid / sparse / rerank through the API) ────────
AUTH = {"Authorization": "Bearer test"}


async def _create_kb(client, name: str) -> dict:  # noqa: ANN001
    resp = await client.post("/v1/kbs", json={"name": name}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _ingest(client, kb_id: str, name: str, content: str) -> None:  # noqa: ANN001
    resp = await client.post(
        f"/v1/kbs/{kb_id}/documents",
        json={"name": name, "content": content, "source_type": "markdown"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_query_hybrid_mode_through_api(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, "hybridkb")
    await _ingest(app_client, kb["kb_id"], "a.md",
                  "The refund policy allows a full refund within 30 days for enterprise plans.")
    await _ingest(app_client, kb["kb_id"], "b.md",
                  "Shipping is free for orders over fifty dollars worldwide.")
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": "refund policy", "top_k": 3, "search_mode": "hybrid"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results
    # The refund chunk is the top hybrid hit (lexical + dense agree).
    assert "refund" in results[0]["content"].lower()


@pytest.mark.asyncio
async def test_query_sparse_mode_through_api(app_client, auth_as) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, "sparsekb")
    await _ingest(app_client, kb["kb_id"], "a.md", "alpha refund beta gamma policy delta.")
    await _ingest(app_client, kb["kb_id"], "b.md", "wholly different unrelated subject matter.")
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": "refund", "top_k": 5, "search_mode": "sparse"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert "refund" in results[0]["content"].lower()


@pytest.mark.asyncio
async def test_query_default_mode_is_dense_and_emits_usage(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client, "densekb")
    content = "The refund policy allows a full refund within 30 days."
    await _ingest(app_client, kb["kb_id"], "a.md", content)
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": content, "top_k": 3, "min_score": 0.5},  # no search_mode -> dense
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0]["score"] >= 0.99  # cosine exact match (dense path unchanged)
    # Usage carries the additive search_mode + reranked units.
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["search_mode"] == "dense"
    assert usage["units"]["reranked"] is False


@pytest.mark.asyncio
async def test_query_rerank_flag_off_is_noop(app_client, auth_as, fake_db) -> None:  # noqa: ANN001
    # Default settings: RAG_RERANK_ENABLED is off, so rerank=true is a no-op (reranked stays false).
    kb = await _create_kb(app_client, "norerankkb")
    await _ingest(app_client, kb["kb_id"], "a.md", "The refund policy is generous.")
    resp = await app_client.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": "refund", "top_k": 3, "rerank": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["reranked"] is False


@pytest.mark.asyncio
async def test_query_rerank_enabled_marks_reranked(app_client_rerank, auth_as, fake_db) -> None:  # noqa: ANN001
    kb = await _create_kb(app_client_rerank, "rerankkb")
    await _ingest(app_client_rerank, kb["kb_id"], "a.md", "The refund policy is generous and clear.")
    await _ingest(app_client_rerank, kb["kb_id"], "b.md", "An unrelated note about something else.")
    resp = await app_client_rerank.post(
        f"/v1/kbs/{kb['kb_id']}/query",
        json={"query": "refund policy", "top_k": 2, "rerank": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert "refund" in results[0]["content"].lower()
    usage = fake_db.outbox_payloads("cypherx.rag.usage.recorded")[-1]
    assert usage["units"]["reranked"] is True
