"""Hybrid retrieval orchestrator.

Fuses three independent legs over the engineering memory and returns a token-bounded,
fully-cited context for the copilot:

  * **graph** — FTS/keyword + natural-key match over the app-owned entity graph
    (``graph_repo.find_entities``).
  * **rag-dense** — dense vector search across the per-tenant RAG KBs
    (``RagClient.query``), the embeddings leg.
  * **keyword** — a second tsvector pass (``graph_repo.keyword_search``) as the BM25-ish leg
    (RAG ships dense-only first cycle, so cypherx-a1 owns keyword).

The legs are fused with **reciprocal-rank fusion** (RRF). RAG hits are mapped back to their
originating graph entity via the ``doc_id`` citation link so a chunk and its entity
reinforce each other (the value of hybrid). Every surviving item becomes a
:class:`~cypherx_a1.models.api.Citation` — answers are never uncited.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any as _Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..core.config import Settings
from ..db import graph_repo, ingest_repo
from ..db.pool import in_tenant
from ..models.api import Citation
from ..services.rag_client import RagClient
from .query_classifier import classify as classify_query
from .query_classifier import leg_weights

logger = structlog.get_logger(__name__)


@dataclass
class EvidenceItem:
    key: str
    kind: str  # 'entity' | 'chunk'
    title: str
    snippet: str = ""
    source: str | None = None
    uri: str | None = None
    entity_id: str | None = None
    entity_kind: str | None = None
    natural_key: str | None = None
    doc_id: str | None = None
    chunk_id: str | None = None
    rrf_score: float = 0.0
    best_dense_score: float | None = None
    # Phase A graph-aware rerank signals: strongest current edge confidence touching this
    # entity, and its creation time (recency). Chunks default to confidence 1.0 / no recency.
    confidence: float = 1.0
    created_at: _Any = None


@dataclass
class RetrievalResult:
    items: list[EvidenceItem] = field(default_factory=list)
    used: dict[str, int] = field(default_factory=dict)

    def context_text(self, max_chars: int = 8000) -> str:
        parts: list[str] = []
        total = 0
        for it in self.items:
            block = f"[{it.title}]"
            if it.snippet:
                block += f"\n{it.snippet}"
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)
        return "\n\n".join(parts)

    def citations(self) -> list[Citation]:
        out: list[Citation] = []
        for it in self.items:
            out.append(
                Citation(
                    kind="chunk" if it.kind == "chunk" else "entity",
                    title=it.title,
                    source=it.source,
                    uri=it.uri,
                    entity_id=it.entity_id,
                    entity_kind=it.entity_kind,
                    natural_key=it.natural_key,
                    doc_id=it.doc_id,
                    chunk_id=it.chunk_id,
                    score=it.best_dense_score,
                    snippet=(it.snippet[:240] or None) if it.snippet else None,
                )
            )
        return out


class RetrievalOrchestrator:
    def __init__(self, settings: Settings, rag: RagClient) -> None:
        self._settings = settings
        self._rag = rag

    async def retrieve(
        self,
        pool: AsyncConnectionPool,
        *,
        tenant_id: str,
        agent_jwt: str,
        agent_id: str | None,
        question: str,
        top_k: int,
    ) -> RetrievalResult:
        s = self._settings

        # ── Legs 1 + 3: graph + keyword (one tenant tx) + the resolved KB ids ──────
        async def _graph(conn: AsyncConnection) -> tuple[list[dict], list[dict], list[str]]:
            graph_hits = await graph_repo.find_entities(conn, query=question, limit=s.retrieval_graph_limit)
            keyword_hits = await graph_repo.keyword_search(conn, query=question, limit=s.retrieval_keyword_limit)
            kbs = await _list_kb_ids(conn)
            return graph_hits, keyword_hits, kbs

        graph_hits, keyword_hits, kb_ids = await in_tenant(pool, tenant_id, _graph)

        # ── Leg 2: RAG dense across the KBs — queried CONCURRENTLY (HTTP, outside any tx) ──
        # Parallel fan-out: latency is one RAG round-trip, not K of them. A forbidden KB or a
        # transport error for one KB is skipped (the others still contribute).
        rag_hits: list[dict] = []
        if kb_ids:
            kb_results = await asyncio.gather(
                *(
                    self._rag.query(
                        kb_id=kb_id, query=question, top_k=top_k, agent_jwt=agent_jwt, on_behalf_of=agent_id
                    )
                    for kb_id in kb_ids
                ),
                return_exceptions=True,
            )
            for res in kb_results:
                if isinstance(res, BaseException) or res is None or res.forbidden:
                    continue
                for h in res.results:
                    rag_hits.append(
                        {"chunk_id": h.chunk_id, "doc_id": h.doc_id, "content": h.content, "score": h.score,
                         "source_name": h.source_name, "source_uri": h.source_uri, "metadata": h.metadata}
                    )

        # Map RAG doc_id -> entity for citation reinforcement.
        doc_ids = [h["doc_id"] for h in rag_hits if h.get("doc_id")]

        async def _docmap(conn: AsyncConnection) -> dict[str, dict]:
            return await ingest_repo.entities_for_docs(conn, doc_ids=doc_ids)

        doc_entity = await in_tenant(pool, tenant_id, _docmap) if doc_ids else {}

        # ── RRF fusion (Phase C: per-leg weights by query intent) ───────────────────
        # Classify the question once and scale each leg's RRF contribution: ownership/
        # dependency questions lean on the graph leg, why/how questions on the RAG leg.
        if s.query_type_weighting_enabled:
            qtype = classify_query(question)
            w_graph, w_keyword, w_rag = leg_weights(qtype)
        else:
            qtype, (w_graph, w_keyword, w_rag) = "general", (1.0, 1.0, 1.0)

        registry: dict[str, EvidenceItem] = {}
        scores: dict[str, float] = {}
        k = s.retrieval_rrf_k

        def _bump(key: str, rank: int, weight: float = 1.0) -> None:
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)

        for rank, row in enumerate(graph_hits):
            key = f"entity:{row['entity_id']}"
            registry.setdefault(key, _entity_item(key, row))
            _bump(key, rank, w_graph)
        for rank, row in enumerate(keyword_hits):
            key = f"entity:{row['entity_id']}"
            registry.setdefault(key, _entity_item(key, row))
            _bump(key, rank, w_keyword)
        for rank, h in enumerate(rag_hits):
            ent = doc_entity.get(h.get("doc_id", ""))
            if ent:
                # A graph entity reinforced by a matching RAG chunk: keep kind='entity' (it IS
                # an entity), attach the chunk's text + doc/chunk ids so the citation carries
                # the actual evidence. (Do NOT relabel kind — that mislabels the provenance.)
                key = f"entity:{ent['entity_id']}"
                item = registry.setdefault(key, _entity_item(key, ent))
                item.doc_id = h.get("doc_id")
                item.chunk_id = h.get("chunk_id")
                item.source = item.source or "rag"
                item.uri = item.uri or h.get("source_uri")
            else:
                key = f"chunk:{h.get('chunk_id')}"
                item = registry.setdefault(
                    key,
                    EvidenceItem(
                        key=key, kind="chunk", title=h.get("source_name") or "knowledge chunk",
                        doc_id=h.get("doc_id"), chunk_id=h.get("chunk_id"), source="rag",
                        uri=h.get("source_uri"),
                    ),
                )
            if not item.snippet:
                item.snippet = (h.get("content") or "")[:600]
            item.best_dense_score = max(item.best_dense_score or 0.0, float(h.get("score") or 0.0))
            _bump(key, rank, w_rag)

        # ── Phase A graph-aware rerank: scale the RRF score by edge confidence x recency ──
        # (1 + w_conf*confidence) * ((1 - w_recency) + w_recency*recency_decay). High-confidence
        # CURRENT edges outrank speculative/stale ones; w_recency=0 disables the time term.
        now = datetime.now(UTC)
        w_conf = s.rerank_confidence_weight
        w_rec = s.rerank_recency_weight
        halflife = max(1.0, s.rerank_recency_halflife_days)
        for key, sc in scores.items():
            it = registry[key]
            it.rrf_score = sc * rerank_multiplier(
                it.confidence, it.created_at, now=now, w_conf=w_conf, w_rec=w_rec, halflife=halflife
            )

        ordered = sorted(registry.values(), key=lambda it: it.rrf_score, reverse=True)
        items = ordered[: s.retrieval_context_max_chunks]
        return RetrievalResult(
            items=items,
            used={"graph": len(graph_hits), "keyword": len(keyword_hits), "rag": len(rag_hits)},
        )


def rerank_multiplier(
    confidence: float,
    created_at: _Any,
    *,
    now: datetime,
    w_conf: float,
    w_rec: float,
    halflife: float,
) -> float:
    """Phase A graph-aware rerank factor: (1 + w_conf*confidence) * ((1-w_rec) + w_rec*recency),
    recency = 0.5 ** (age_days / halflife). Pure + unit-testable."""
    recency = 1.0
    if created_at is not None:
        try:
            age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
            recency = 0.5 ** (age_days / max(1.0, halflife))
        except (TypeError, ValueError):
            recency = 1.0
    return (1.0 + w_conf * confidence) * ((1.0 - w_rec) + w_rec * recency)


def _entity_item(key: str, row: dict) -> EvidenceItem:
    attrs = row.get("attrs") or {}
    return EvidenceItem(
        key=key,
        kind="entity",
        title=row.get("title") or row.get("natural_key") or "entity",
        snippet=(row.get("search_text") or "")[:400],
        source=row.get("source"),
        uri=attrs.get("url"),
        entity_id=str(row["entity_id"]),
        entity_kind=row.get("kind"),
        natural_key=row.get("natural_key"),
        confidence=float(row.get("edge_confidence") or 1.0),
        created_at=row.get("created_at"),
    )


async def _list_kb_ids(conn: AsyncConnection) -> list[str]:
    cur = await conn.execute("SELECT kb_id FROM cypherx_a1.rag_kbs")
    return [r[0] for r in await cur.fetchall()]
