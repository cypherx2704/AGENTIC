"""Retrieval / query (Component 4) — POST /v1/kbs/{kb_id}/query.

Pipeline: auth (rag:query) -> KB exists -> ACL check (403 FORBIDDEN_KB on deny) -> quota
queries/min (429) -> embed the query (llms or mock) -> two-pass vector search (PgVectorAdapter
CTE, SET LOCAL hnsw.ef_search) -> usage event (units + request_id) -> response with duration_ms.

``top_k`` is server-capped (over -> VALIDATION_ERROR); ``ef_search`` is clamped to its cap.
"""

from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, Request

from ..core import trace
from ..core.auth import SCOPE_QUERY, Principal, require_scope
from ..core.errors import ApiError, ErrorCode, parse_uuid
from ..db import outbox, repository
from ..models.api import QueryHit, QueryHitSource, QueryRequest, QueryResponse
from ..services import acl, quota
from ..services.acl import OP_QUERY
from ..services.store import resolve_vector_store

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

    from ..core import metrics

    # ── Dense leg query embedding (skipped for sparse-only retrieval) ──────────
    query_vector: list[float] | None = None
    if body.search_mode != "sparse":
        embedder = request.app.state.embedder
        result = await embedder.embed(
            [body.query],
            model=kb["embedding_model_resolved"],
            dim=kb["embedding_dim"],
            agent_jwt=agent_jwt,
            on_behalf_of=on_behalf_of,
        )
        query_vector = result.vectors[0]

    if body.search_mode == "dense":
        # UNCHANGED two-pass dense path (the verified default). min_score floors cosine.
        # When reranking, the cosine floor is relaxed so the candidate pool isn't starved
        # before the reranker (the rerank stage does the final relevance gating); the default
        # (non-rerank) call keeps body.min_score exactly as today.
        dense_min_score = -1.0 if rerank_active else body.min_score
        hits = await store.search(
            principal.tenant_id,
            kb_id,
            query_vector,  # type: ignore[arg-type] — always set on the dense path
            top_k=retrieve_k,
            min_score=dense_min_score,
            filters=body.filters,
            dimension=kb["embedding_dim"],
            ef_search=ef_search,
        )
    else:
        # Hybrid / sparse: dense + lexical fused with RRF in SQL. The fused score is a rank-
        # fusion score (not cosine), so min_score (a cosine floor) is NOT applied here.
        candidates = min(
            body.top_k * settings.hybrid_candidate_multiplier, settings.hybrid_candidate_cap
        )
        candidates = max(candidates, retrieve_k)
        hits = await store.search_hybrid(
            principal.tenant_id,
            kb_id,
            query_vector,
            body.query,
            top_k=retrieve_k,
            candidates=candidates,
            rrf_k=settings.hybrid_rrf_k,
            filters=body.filters,
            dimension=kb["embedding_dim"],
            ef_search=ef_search,
            mode=body.search_mode,
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
    await _emit_usage(request, principal, kb_id, body, len(hits), reranked=reranked)

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


async def _emit_usage(
    request: Request,
    principal: Principal,
    kb_id: str,
    body: QueryRequest,
    returned: int,
    *,
    reranked: bool = False,
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
            },
            agent_id=principal.agent_id,
            api_key_id=principal.api_key_id,
            producer_version=settings.service_version,
        )
    except Exception as exc:  # noqa: BLE001 — metering must never fail the query
        logger.warning("query_usage_emit_failed", error=str(exc))
