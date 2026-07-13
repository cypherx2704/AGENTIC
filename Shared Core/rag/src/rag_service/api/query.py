"""Retrieval / query (Component 4) — POST /v1/kbs/{kb_id}/query.

Pipeline: auth (rag:query) -> KB exists -> ACL check (403 FORBIDDEN_KB on deny) -> quota
queries/min (429) -> embed the query (llms or mock) -> two-pass vector search (PgVectorAdapter
CTE, SET LOCAL hnsw.ef_search) -> usage event (units + request_id) -> response with duration_ms.

``top_k`` is server-capped (over -> VALIDATION_ERROR); ``ef_search`` is clamped to its cap.
"""

from __future__ import annotations

import time
from dataclasses import replace

import structlog
from fastapi import APIRouter, Depends, Request

from ..core import trace
from ..core.auth import SCOPE_QUERY, Principal, require_scope
from ..core.errors import ApiError, ErrorCode, parse_uuid
from ..db import outbox, repository
from ..models.api import QueryHit, QueryHitSource, QueryRequest, QueryResponse
from ..services import acl, quota
from ..services.acl import OP_QUERY
from ..services.fusion import reciprocal_rank_fusion
from ..services.store import resolve_vector_store
from ..services.store.base import ChunkHit

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/kbs", tags=["query"])


def _agent_jwt(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-agent-jwt")
    if fwd:
        return fwd
    auth = request.headers.get("authorization", "")
    return auth.partition(" ")[2].strip() or None


@router.post("/{kb_id}/query", response_model=None)
async def query_kb(
    kb_id: str,
    body: QueryRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_QUERY)),
) -> QueryResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    settings = request.app.state.settings
    pool = getattr(request.app.state, "db_pool", None)
    valkey = getattr(request.app.state, "valkey", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Database is not available.")

    # top_k server cap (over -> VALIDATION_ERROR per the checklist).
    if body.top_k > settings.query_top_k_cap:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"top_k {body.top_k} exceeds the server cap of {settings.query_top_k_cap}.",
            details={"reason": "TOP_K_EXCEEDED", "cap": settings.query_top_k_cap},
        )

    kb = await repository.get_kb(pool, principal.tenant_id, kb_id)
    if kb is None:
        from ..core import metrics

        metrics.query_total.labels("not_found").inc()
        raise ApiError(ErrorCode.NOT_FOUND, "Knowledge base not found.")

    # KB ACL — 403 FORBIDDEN_KB on deny.
    await acl.check_access(pool, principal, kb_id, OP_QUERY, settings=settings)

    # Quota: queries/min (429 over).
    await quota.enforce_queries_per_min(valkey, principal, settings=settings)

    ef_search = min(body.ef_search or settings.hnsw_ef_search_default, settings.hnsw_ef_search_cap)

    started = time.monotonic()
    store = await resolve_vector_store(pool, settings, principal.tenant_id)
    agent_jwt = _agent_jwt(request)
    on_behalf_of = principal.agent_id or principal.on_behalf_of

    # Rerank (optional): flag-gated AND opt-in. When active we retrieve a wider candidate pool
    # then re-order it down to top_k. Default (flag off OR rerank=false) ⇒ today's behaviour.
    rerank_active = settings.rag_rerank_enabled and body.rerank
    retrieve_k = min(settings.rerank_candidate_n, settings.query_top_k_cap) if rerank_active else body.top_k
    retrieve_k = max(retrieve_k, body.top_k)

    # Query-transformation opt-ins (each flag-gated AND per-query, mirroring the rerank guard).
    # Default (flag off OR field false) ⇒ the single-query path below runs UNCHANGED. decompose
    # takes precedence over multi_query when a caller opts into both.
    decompose_active = settings.rag_decompose_enabled and body.decompose
    multiquery_active = settings.rag_multiquery_enabled and body.multi_query

    from ..core import metrics

    decomposed = False
    expanded = False
    hits: list[ChunkHit] | None = None
    if decompose_active and getattr(request.app.state, "decomposer", None) is not None:
        hits, decomposed = await _retrieve_decomposed(
            request, body, kb, kb_id, store, settings,
            tenant_id=principal.tenant_id, retrieve_k=retrieve_k, ef_search=ef_search,
            rerank_active=rerank_active, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
        )
    elif multiquery_active and getattr(request.app.state, "expander", None) is not None:
        hits, expanded = await _retrieve_multiquery(
            request, body, kb, kb_id, store, settings,
            tenant_id=principal.tenant_id, retrieve_k=retrieve_k, ef_search=ef_search,
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
        )

    if hits is None:
        # ── Single-query retrieval (the verified DEFAULT path — byte-identical) ──────
        # min_score floors cosine on the dense path; when reranking, the floor is relaxed so the
        # candidate pool isn't starved before the reranker (rerank does the final gating). The
        # hybrid/sparse fused score is a rank-fusion score (not cosine), so no floor is applied.
        vectors = await _embed_queries(
            request, [body.query], kb, search_mode=body.search_mode,
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
        )
        hits = await _search_once(
            store, principal.tenant_id, kb_id, settings,
            query_text=body.query, query_vector=vectors[0], retrieve_k=retrieve_k, body=body,
            dimension=kb["embedding_dim"], ef_search=ef_search,
            dense_min_score=(-1.0 if rerank_active else body.min_score),
        )
    metrics.query_search_mode_total.labels(body.search_mode).inc()

    reranked = False
    if rerank_active and hits:
        hits, reranked = await _maybe_rerank(
            request, body.query, hits, top_k=body.top_k,
            agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
        )
    else:
        # No rerank: trim a (possibly wider) candidate pool back to the requested top_k.
        hits = hits[: body.top_k]

    duration_ms = int((time.monotonic() - started) * 1000)

    # Usage metering: units + request_id ONLY (Contract-14 single-owner rule).
    await _emit_usage(
        request, principal, kb_id, body, len(hits),
        reranked=reranked, decomposed=decomposed, expanded=expanded,
    )

    metrics.query_total.labels("ok").inc()
    metrics.query_duration_seconds.observe(duration_ms / 1000)
    metrics.chunks_returned.observe(len(hits))

    results = [
        QueryHit(
            chunk_id=h.chunk_id,
            doc_id=h.doc_id,
            content=h.content,
            score=round(h.score, 6),
            metadata={k: v for k, v in (h.metadata or {}).items() if k != "content_sha"},
            source=QueryHitSource(
                name=(h.metadata or {}).get("doc_name", ""),
                uri=(h.metadata or {}).get("source_uri"),
            ),
        )
        for h in hits
    ]
    return QueryResponse(results=results, duration_ms=duration_ms)


async def _maybe_rerank(
    request: Request,
    query: str,
    hits: list,  # noqa: ANN001 — list[ChunkHit]
    *,
    top_k: int,
    agent_jwt: str | None,
    on_behalf_of: str | None,
) -> tuple[list, bool]:
    """Re-order ``hits`` via the rerank client, returning the top_k + whether it ran.

    Mock-tolerant + fail-soft: if the client falls back to the base ordering (gateway down)
    the original ranking is preserved and the reranked usage flag reflects the real source.
    """
    reranker = getattr(request.app.state, "reranker", None)
    if reranker is None:
        return hits[:top_k], False
    from ..core import metrics

    documents = [h.content for h in hits]
    result = await reranker.rerank(
        query, documents, top_n=top_k, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of
    )
    metrics.query_rerank_total.labels(result.source).inc()
    # 'fallback_base' = the gateway was unavailable and we kept the base ordering: not a real
    # rerank, so do NOT bill it as reranked.
    if result.source == "fallback_base" or not result.items:
        return hits[:top_k], False
    reordered = [hits[it.index] for it in result.items if 0 <= it.index < len(hits)]
    return reordered[:top_k], True


async def _embed_queries(
    request: Request,
    texts: list[str],
    kb: dict,
    *,
    search_mode: str,
    agent_jwt: str | None,
    on_behalf_of: str | None,
) -> list[list[float] | None]:
    """Embed a batch of query strings (dense leg). Sparse-only retrieval needs no vectors, so
    it returns ``[None] * len(texts)`` (the lexical leg carries the query text). Used by the
    single-query, decompose (per sub-question), and multi-query (per variant) paths alike."""
    if search_mode == "sparse":
        return [None] * len(texts)
    result = await request.app.state.embedder.embed(
        texts,
        model=kb["embedding_model_resolved"],
        dim=kb["embedding_dim"],
        agent_jwt=agent_jwt,
        on_behalf_of=on_behalf_of,
    )
    return list(result.vectors)


async def _search_once(
    store,  # noqa: ANN001 — IVectorStore
    tenant_id: str,
    kb_id: str,
    settings,  # noqa: ANN001 — Settings
    *,
    query_text: str,
    query_vector: list[float] | None,
    retrieve_k: int,
    body: QueryRequest,
    dimension: int,
    ef_search: int,
    dense_min_score: float,
) -> list[ChunkHit]:
    """One retrieval for ``(query_text, query_vector)`` honouring ``body.search_mode``.

    This is the single retrieval primitive the default path, decompose, and multi-query all
    share, so their per-query retrieval is byte-identical to today's dense/hybrid/sparse legs.
    The dense two-pass CTE keeps its cosine floor; hybrid/sparse fuse in SQL (no cosine floor).
    """
    if body.search_mode == "dense":
        return await store.search(
            tenant_id, kb_id, query_vector,
            top_k=retrieve_k, min_score=dense_min_score, filters=body.filters,
            dimension=dimension, ef_search=ef_search,
        )
    candidates = min(body.top_k * settings.hybrid_candidate_multiplier, settings.hybrid_candidate_cap)
    candidates = max(candidates, retrieve_k)
    return await store.search_hybrid(
        tenant_id, kb_id, query_vector, query_text,
        top_k=retrieve_k, candidates=candidates, rrf_k=settings.hybrid_rrf_k,
        filters=body.filters, dimension=dimension, ef_search=ef_search, mode=body.search_mode,
    )


async def _retrieve_decomposed(
    request: Request,
    body: QueryRequest,
    kb: dict,
    kb_id: str,
    store,  # noqa: ANN001 — IVectorStore
    settings,  # noqa: ANN001 — Settings
    *,
    tenant_id: str,
    retrieve_k: int,
    ef_search: int,
    rerank_active: bool,
    agent_jwt: str | None,
    on_behalf_of: str | None,
) -> tuple[list[ChunkHit], bool]:
    """Multi-hop retrieval (B2): decompose → retrieve per sub-question → union+dedup by chunk_id.

    Returns ``(merged_pool, decomposed)`` where ``decomposed`` is True only when the query
    actually split into >1 sub-question (a non-decomposable query or a gateway failure degrades
    to single-query retrieval, so the result equals today's path). The merged pool is capped at
    ``retrieve_k`` and handed to the shared rerank/trim stage by the caller. Facts scattered
    across separate chunks are each retrieved by their own focused sub-question, then unioned —
    keeping the best (max) per-chunk score across sub-questions for deterministic ordering."""
    from ..core import metrics

    sub_questions, source = await request.app.state.decomposer.decompose(
        body.query, model=settings.decompose_model, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
    )
    metrics.query_decompose_total.labels(source).inc()

    vectors = await _embed_queries(
        request, sub_questions, kb, search_mode=body.search_mode,
        agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
    )
    dense_min_score = -1.0 if rerank_active else body.min_score
    merged: dict[str, ChunkHit] = {}
    for text, vector in zip(sub_questions, vectors, strict=True):
        for hit in await _search_once(
            store, tenant_id, kb_id, settings,
            query_text=text, query_vector=vector, retrieve_k=retrieve_k, body=body,
            dimension=kb["embedding_dim"], ef_search=ef_search, dense_min_score=dense_min_score,
        ):
            prev = merged.get(hit.chunk_id)
            if prev is None or hit.score > prev.score:
                merged[hit.chunk_id] = hit
    pool = sorted(merged.values(), key=lambda h: (h.score, h.chunk_id), reverse=True)[:retrieve_k]
    return pool, len(sub_questions) > 1


async def _retrieve_multiquery(
    request: Request,
    body: QueryRequest,
    kb: dict,
    kb_id: str,
    store,  # noqa: ANN001 — IVectorStore
    settings,  # noqa: ANN001 — Settings
    *,
    tenant_id: str,
    retrieve_k: int,
    ef_search: int,
    agent_jwt: str | None,
    on_behalf_of: str | None,
) -> tuple[list[ChunkHit], bool]:
    """Multi-query expansion / RAG-Fusion (B3): expand → retrieve per variant → fuse with RRF.

    Returns ``(fused_pool, expanded)``. Each variant's ranked list is fused with the app-level
    Reciprocal Rank Fusion (``services/fusion.py``, k=``hybrid_rrf_k``) — a recall lever for
    vocabulary mismatch. The returned hits carry the fused RRF score (a rank-fusion score, not a
    cosine), matching the hybrid path's score semantics. A gateway failure degrades to the
    original single query (``expanded=False``). The pool is handed to the shared rerank/trim
    stage — pair with rerank to restore top-k precision (the full RAG-Fusion recipe)."""
    from ..core import metrics

    variants, source = await request.app.state.expander.expand(
        body.query, n=settings.multiquery_num_variants, model=settings.multiquery_model,
        agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
    )
    metrics.query_multiquery_total.labels(source).inc()

    vectors = await _embed_queries(
        request, variants, kb, search_mode=body.search_mode,
        agent_jwt=agent_jwt, on_behalf_of=on_behalf_of,
    )
    ranked_lists: list[list[str]] = []
    pool: dict[str, ChunkHit] = {}
    for text, vector in zip(variants, vectors, strict=True):
        hits_i = await _search_once(
            store, tenant_id, kb_id, settings,
            query_text=text, query_vector=vector, retrieve_k=retrieve_k, body=body,
            dimension=kb["embedding_dim"], ef_search=ef_search, dense_min_score=-1.0,
        )
        ranked_lists.append([h.chunk_id for h in hits_i])
        for hit in hits_i:
            pool.setdefault(hit.chunk_id, hit)
    fused = reciprocal_rank_fusion(ranked_lists, k=settings.hybrid_rrf_k)
    hits = [replace(pool[cid], score=score) for cid, score in fused[:retrieve_k] if cid in pool]
    return hits, len(variants) > 1


async def _emit_usage(
    request: Request,
    principal: Principal,
    kb_id: str,
    body: QueryRequest,
    returned: int,
    *,
    reranked: bool = False,
    decomposed: bool = False,
    expanded: bool = False,
) -> None:
    pool = getattr(request.app.state, "db_pool", None)
    settings = request.app.state.settings
    if pool is None:
        return
    try:
        await outbox.emit_usage(
            pool,
            tenant_id=principal.tenant_id,
            trace_id=trace.trace_id_var.get(),
            request_id=trace.request_id_var.get(),
            operation="rag.query",
            units={
                "chunks_returned": returned,
                "top_k": body.top_k,
                "search_mode": body.search_mode,
                "reranked": reranked,
                "decomposed": decomposed,
                "expanded": expanded,
            },
            agent_id=principal.agent_id,
            api_key_id=principal.api_key_id,
            producer_version=settings.service_version,
        )
    except Exception as exc:  # noqa: BLE001 — metering must never fail the query
        logger.warning("query_usage_emit_failed", error=str(exc))
