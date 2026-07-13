"""Memory store / retrieve / by-id endpoints.

Store lifecycle (POST /v1/memories), in this exact order so a replay never re-embeds:

    auth (mem:write) -> content cap (413) -> Idempotency-Key short-circuit (replay/409)
    BEFORE embedding -> resolve tenant policy + limits -> quota (rate + resource caps)
    -> embed (gateway or mock) -> store with dedup-bump (one txn: row + vector + event)
    -> idempotency complete -> 201.

Retrieve (POST /v1/memories/search):

    auth (mem:read) -> embed query -> two-pass vector search with the SAME visibility
    predicate that makes the cross-end-user leak impossible -> inline last_accessed bump.

By-id (GET/PUT/DELETE /v1/memories/{id}): a memory the caller cannot SEE is 404, never
403 (anti-existence-leak). PUT rejects immutable fields; mutation is owner-only.
"""

from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core import metrics, trace
from ..core.auth import SCOPE_READ, SCOPE_WRITE, Principal, require_scope
from ..core.errors import ApiError, ErrorCode
from ..models.memory import (
    MemoryRecord,
    SearchMemoryRequest,
    SearchMemoryResponse,
    StoreMemoryRequest,
    UpdateMemoryRequest,
)
from ..services import extraction, idempotency, quota, repository, scoring
from ..services.scoring import weights_from_settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["memories"])


def _repo(request: Request) -> repository.MemoryRepository:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Memory backend is unavailable.")
    return repo


def _embedder(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.embedder


def _agent_jwt(request: Request) -> str | None:
    """The raw inbound agent JWT (already verified by the auth dependency) to forward to the
    llms-gateway embeddings call so it resolves the caller tenant's BYOK key."""
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def _settings(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.settings


async def _emit_usage(
    request: Request,
    principal: Principal,
    *,
    operation: str,
    units: dict[str, float],
    duration_ms: int | None = None,
) -> None:
    """Emit a Contract-19.1 ``cypherx.memory.usage.recorded`` event via the outbox.

    Best-effort + fail-soft: a metering write must NEVER fail the request. With a DB pool
    it inserts an outbox row in its own short tenant transaction (the usage event is a
    sidecar to the operation, not part of its atomic txn). With no pool (tests / in-memory
    degradation) it appends to the in-memory repo's ``events`` list for introspection.
    """
    from ..services import usage

    settings = _settings(request)
    if not getattr(settings, "memory_usage_events_enabled", True):
        return
    try:
        payload = usage.build_usage_payload(
            principal=principal, operation=operation, units=units,
            trace_id=trace.trace_id_var.get(), duration_ms=duration_ms,
        )
        pool = getattr(request.app.state, "db_pool", None)
        if pool is not None:
            from ..db import outbox
            from ..db.pool import in_tenant

            async def _fn(conn):  # type: ignore[no-untyped-def]
                await outbox.emit(
                    conn, topic=outbox.TOPIC_MEMORY_USAGE_RECORDED,
                    tenant_id=principal.tenant_id, trace_id=trace.trace_id_var.get(),
                    payload=payload, producer_version=settings.service_version,
                )

            await in_tenant(pool, principal.tenant_id, _fn)
        else:
            repo = getattr(request.app.state, "repo", None)
            events = getattr(repo, "events", None)
            if events is not None:
                events.append(
                    {"topic": "cypherx.memory.usage.recorded",
                     "tenant_id": principal.tenant_id, "operation": operation,
                     "units": payload["units"]}
                )
    except Exception as exc:  # noqa: BLE001 — metering must never fail the request
        logger.warning("usage_event_emit_failed", operation=operation, error=str(exc))
        metrics.store_billing_write_failed_total.labels("usage_event").inc()


async def _grade_importance_llm(request: Request, content: str, memory_type: str) -> float | None:
    """Optional LLM importance grade (behind MEMORY_IMPORTANCE_LLM_ENABLED).

    Skeleton: there is no dedicated importance endpoint on the llms-gateway in this cycle,
    so this returns None (caller keeps the deterministic heuristic). Wiring a real grader
    is a follow-up; the flag + seam exist so it is additive when added.
    """
    return None


async def _extract_facts(request: Request, settings, content: str) -> list[str]:
    """Resolve incoming content into atomic facts (B5). Fails SOFT to the raw content.

    Prefers the llms-gateway seam when MEMORY_EXTRACTION_LLM_ENABLED (it is a default-off
    skeleton this cycle), else the deterministic split. On ANY extractor error the raw
    content is returned as one fact, so the store degrades to today's single-row behavior.
    """
    try:
        if settings.memory_extraction_llm_enabled:
            llm = await extraction.extract_facts_llm(
                _embedder(request), content, max_facts=settings.memory_extraction_max_facts
            )
            if llm:
                return llm
        return extraction.extract_facts(content, max_facts=settings.memory_extraction_max_facts)
    except Exception as exc:  # noqa: BLE001 — extraction must never fail the store
        logger.warning("extraction_failed_store_raw", error=str(exc))
        metrics.extraction_failopen_total.inc()
        return [content]


async def _store_extracted_facts(
    request: Request,
    principal: Principal,
    *,
    body: StoreMemoryRequest,
    facts: list[str],
    ptype: str,
    pid: str,
    idem_key: str | None,
    started: float,
) -> JSONResponse:
    """B5 fan-out: store each extracted fact as its own row + focused embedding.

    Design points handled: (1) 201 response is an aggregate ``{extracted, count, memories}``
    (single-fact / disabled requests keep the flat single-record shape); (2) per-fact
    dedup-bump — each fact deduplicates independently against the principal's neighbours;
    (3) aggregate usage metering — ONE write event for the request, ``items_written`` = the
    count actually inserted (deduped facts don't count); (4) idempotency replay stores +
    replays the aggregate body verbatim. Runs AFTER the idempotency short-circuit, so a
    replay never re-embeds.
    """
    settings = _settings(request)
    repo = _repo(request)
    valkey = getattr(request.app.state, "valkey", None)
    threshold = await repo.get_tenant_dedup_threshold(principal.tenant_id, settings.dedup_threshold)

    records: list[dict] = []
    inserted = 0
    total_chars = 0
    for fact in facts:
        vector, _source = await _embedder(request).embed_one(
            fact, on_behalf_of=principal.agent_id, agent_jwt=_agent_jwt(request)
        )
        importance = body.importance
        if importance is None:
            importance = scoring.heuristic_importance(fact, memory_type=body.type)
        mem = repository.new_memory(
            tenant_id=principal.tenant_id, principal_type=ptype, principal_id=pid,
            scope=body.scope, type=body.type, tags=body.tags, content=fact,
            metadata=body.metadata, vector=vector, session_id=body.session_id,
            ttl_seconds=body.ttl_seconds, importance_score=importance,
            session_scope_id=body.session_scope_id, agent_scope_id=body.agent_scope_id,
        )
        result = await repo.store(
            memory=mem, dedup_threshold=threshold, trace_id=trace.trace_id_var.get(),
            producer_version=settings.service_version,
            linking_enabled=settings.memory_linking_enabled,
            linking_sim_min=settings.memory_linking_sim_min,
            linking_max_neighbors=settings.memory_linking_max_neighbors,
        )
        if result.deduped:
            metrics.dedup_bumped_total.inc()
        else:
            inserted += 1
        total_chars += len(fact)
        records.append(
            MemoryRecord(**repository.to_wire(result.memory, deduped=result.deduped)).model_dump()
        )

    metrics.extraction_facts_total.inc(len(facts))
    await _emit_usage(
        request, principal, operation="write",
        units={"items_written": float(inserted), "embedding_tokens": float(total_chars),
               "facts_extracted": float(len(facts))},
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    body_dict = {"extracted": True, "count": len(records), "memories": records}
    if idem_key:
        await idempotency.complete(valkey, idem_key, principal, 201, body_dict)
    metrics.requests_total.labels("store", "success").inc()
    metrics.request_duration_seconds.labels("store").observe(time.monotonic() - started)
    return JSONResponse(content=body_dict, status_code=201)


# ── Store ──────────────────────────────────────────────────────────────────────────
@router.post("/memories", status_code=201, response_model=None)
async def store_memory(
    body: StoreMemoryRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_WRITE)),
) -> JSONResponse:
    settings = _settings(request)
    repo = _repo(request)
    valkey = getattr(request.app.state, "valkey", None)
    pool = getattr(request.app.state, "db_pool", None)
    started = time.monotonic()

    # ── Content cap (16 KiB) — cheap, BEFORE any backend work ─────────────────────
    content_bytes = len(body.content.encode("utf-8"))
    if content_bytes > settings.content_max_bytes:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"content is {content_bytes} bytes; the maximum is {settings.content_max_bytes}.",
            status_code=413,
            details={"reason": "CONTENT_TOO_LARGE", "bytes": content_bytes,
                     "max_bytes": settings.content_max_bytes},
        )

    # ── Idempotency-Key short-circuit — BEFORE embedding (no double-embed on replay) ─
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        state = await idempotency.begin(valkey, idem_key, principal)
        if state is idempotency.BeginState.IN_FLIGHT:
            idempotency.raise_in_flight()  # 409
        if state is idempotency.BeginState.COMPLETED:
            replay = await idempotency.get_replay(valkey, idem_key, principal)
            if replay is not None:
                return JSONResponse(
                    content=replay.body, status_code=replay.status_code,
                    headers={idempotency.REPLAY_HEADER: "true"},
                )

    ptype, pid = principal.memory_principal

    # ── Quota: rate cap (stores_per_min) + resource caps (memories_max/storage) ─────
    limits = await quota.resolve_limits(principal, pool=pool, settings=settings)
    await quota.enforce_rate(
        valkey, principal, dimension="stores_per_min", limit=limits.stores_per_min, settings=settings
    )
    if pool is not None:
        cur_count, cur_bytes = await repo.resource_usage(principal.tenant_id, ptype, pid)
        quota.enforce_resource_caps(
            limits=limits, current_count=cur_count, current_bytes=cur_bytes,
            new_content_bytes=content_bytes,
        )

    # ── B5: salient-fact extraction fan-out (flag-gated; AFTER idempotency, BEFORE embed) ─
    # Decompose multi-fact content into atomic facts, each its own row + focused embedding.
    # Off / single-fact => falls through to the byte-identical single-memory path below.
    if settings.memory_extraction_enabled:
        facts = await _extract_facts(request, settings, body.content)
        if len(facts) > 1:
            return await _store_extracted_facts(
                request, principal, body=body, facts=facts, ptype=ptype, pid=pid,
                idem_key=idem_key, started=started,
            )

    # ── Embed (gateway or deterministic mock) ──────────────────────────────────────
    vector, _source = await _embedder(request).embed_one(
        body.content, on_behalf_of=principal.agent_id, agent_jwt=_agent_jwt(request)
    )

    # ── Importance (caller override -> heuristic -> optional LLM grade) ─────────────
    importance = body.importance
    if importance is None:
        importance = scoring.heuristic_importance(body.content, memory_type=body.type)
        if settings.memory_importance_llm_enabled:
            graded = await _grade_importance_llm(request, body.content, body.type)
            if graded is not None:
                importance = graded

    # ── Store with dedup-bump (single txn: row + vector + stored event) ────────────
    threshold = await repo.get_tenant_dedup_threshold(principal.tenant_id, settings.dedup_threshold)
    mem = repository.new_memory(
        tenant_id=principal.tenant_id, principal_type=ptype, principal_id=pid, scope=body.scope,
        type=body.type, tags=body.tags, content=body.content, metadata=body.metadata,
        vector=vector, session_id=body.session_id, ttl_seconds=body.ttl_seconds,
        importance_score=importance, session_scope_id=body.session_scope_id,
        agent_scope_id=body.agent_scope_id,
    )
    result = await repo.store(
        memory=mem, dedup_threshold=threshold, trace_id=trace.trace_id_var.get(),
        producer_version=settings.service_version,
        linking_enabled=settings.memory_linking_enabled,
        linking_sim_min=settings.memory_linking_sim_min,
        linking_max_neighbors=settings.memory_linking_max_neighbors,
    )
    if result.deduped:
        metrics.dedup_bumped_total.inc()

    # ── Contract-19.1 usage metering (additive; via the outbox) ────────────────────
    await _emit_usage(
        request, principal, operation="write",
        units={"items_written": 0.0 if result.deduped else 1.0,
               "embedding_tokens": float(len(body.content))},
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    record = MemoryRecord(**repository.to_wire(result.memory, deduped=result.deduped))
    body_dict = record.model_dump()

    if idem_key:
        await idempotency.complete(valkey, idem_key, principal, 201, body_dict)

    metrics.requests_total.labels("store", "success").inc()
    metrics.request_duration_seconds.labels("store").observe(time.monotonic() - started)
    return JSONResponse(content=body_dict, status_code=201)


# ── Search ─────────────────────────────────────────────────────────────────────────
@router.post("/memories/search", response_model=None)
async def search_memories(
    body: SearchMemoryRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_READ)),
) -> JSONResponse:
    settings = _settings(request)
    repo = _repo(request)
    valkey = getattr(request.app.state, "valkey", None)
    pool = getattr(request.app.state, "db_pool", None)
    started = time.monotonic()

    # Rate cap (retrieves_per_min). Resource caps don't apply to reads.
    limits = await quota.resolve_limits(principal, pool=pool, settings=settings)
    await quota.enforce_rate(
        valkey, principal, dimension="retrieves_per_min", limit=limits.retrieves_per_min,
        settings=settings,
    )

    top_k = min(body.top_k, settings.search_top_k_max)
    ptype, pid = principal.memory_principal
    visibility = await repo.get_tenant_visibility(principal.tenant_id)

    # ── Temporal-validity filter: per-request override, else the server flag ─────────
    if body.include_superseded is None:
        current_only = settings.memory_search_current_only
    else:
        current_only = not body.include_superseded

    query_vector, _source = await _embedder(request).embed_one(
        body.query, on_behalf_of=principal.agent_id, agent_jwt=_agent_jwt(request)
    )
    results = await repo.search(
        tenant_id=principal.tenant_id, caller_type=ptype, caller_id=pid,
        query_vector=query_vector, top_k=top_k, type_filter=body.type, tags_filter=body.tags,
        include_shared=body.include_shared, user_scope_visibility=visibility,
        scoring_enabled=settings.memory_scoring_enabled,
        scoring_weights=weights_from_settings(settings),
        current_only=current_only,
        session_scope_id=body.session_scope_id,
        agent_scope_id=body.agent_scope_id,
        mmr_enabled=settings.memory_mmr_enabled,
        mmr_lambda=settings.memory_mmr_lambda,
        linking_enabled=settings.memory_linking_enabled,
        link_expansion_limit=settings.memory_linking_expansion_limit,
    )
    payload = SearchMemoryResponse(
        results=[MemoryRecord(**repository.to_wire(m)) for m in results],
        count=len(results),
    )
    if settings.memory_scoring_enabled:
        metrics.scoring_reranked_total.inc()

    # ── Contract-19.1 usage metering for the recall (+ a 'score' op when re-ranking) ─
    await _emit_usage(
        request, principal, operation="recall",
        units={"items_recalled": float(len(results))},
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if settings.memory_scoring_enabled and results:
        await _emit_usage(
            request, principal, operation="score",
            units={"items_scored": float(len(results))},
        )

    metrics.requests_total.labels("search", "success").inc()
    metrics.request_duration_seconds.labels("search").observe(time.monotonic() - started)
    return JSONResponse(content=payload.model_dump())


# ── By-id: GET ───────────────────────────────────────────────────────────────────
@router.get("/memories/{memory_id}", response_model=None)
async def get_memory(
    memory_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_READ)),
) -> JSONResponse:
    repo = _repo(request)
    ptype, pid = principal.memory_principal
    visibility = await repo.get_tenant_visibility(principal.tenant_id)
    m = await repo.get_by_id(
        tenant_id=principal.tenant_id, caller_type=ptype, caller_id=pid, memory_id=memory_id,
        user_scope_visibility=visibility,
    )
    if m is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Memory not found.")  # 404, not 403
    return JSONResponse(content=MemoryRecord(**repository.to_wire(m)).model_dump())


# ── By-id: PUT (immutable-field rejection; owner-only) ───────────────────────────
@router.put("/memories/{memory_id}", response_model=None)
async def update_memory(
    memory_id: str,
    body: UpdateMemoryRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_WRITE)),
) -> JSONResponse:
    settings = _settings(request)
    repo = _repo(request)
    ptype, pid = principal.memory_principal

    changes = body.model_dump(exclude_unset=True)
    if "content" in changes and changes["content"] is not None:
        content_bytes = len(changes["content"].encode("utf-8"))
        if content_bytes > settings.content_max_bytes:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"content is {content_bytes} bytes; the maximum is {settings.content_max_bytes}.",
                status_code=413,
                details={"reason": "CONTENT_TOO_LARGE", "bytes": content_bytes},
            )
    # Translate ttl_seconds -> absolute expires_at for the repo.
    if "ttl_seconds" in changes:
        ttl = changes.pop("ttl_seconds")
        if ttl is not None:
            from datetime import UTC, datetime, timedelta

            changes["expires_at"] = datetime.now(UTC) + timedelta(seconds=ttl)

    updated = await repo.update(
        tenant_id=principal.tenant_id, caller_type=ptype, caller_id=pid, memory_id=memory_id,
        changes=changes,
    )
    if updated is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Memory not found.")  # 404 (owner-only mutation)
    return JSONResponse(content=MemoryRecord(**repository.to_wire(updated)).model_dump())


# ── By-id: DELETE (owner-only; 404 when invisible) ───────────────────────────────
@router.delete("/memories/{memory_id}", response_model=None)
async def delete_memory(
    memory_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_WRITE)),
) -> JSONResponse:
    settings = _settings(request)
    repo = _repo(request)
    ptype, pid = principal.memory_principal
    deleted = await repo.delete(
        tenant_id=principal.tenant_id, caller_type=ptype, caller_id=pid, memory_id=memory_id,
        trace_id=trace.trace_id_var.get(), producer_version=settings.service_version,
    )
    if not deleted:
        raise ApiError(ErrorCode.NOT_FOUND, "Memory not found.")
    # ── Contract-19.1 usage metering (additive; via the outbox) ────────────────────
    await _emit_usage(
        request, principal, operation="delete", units={"items_deleted": 1.0},
    )
    return JSONResponse(content={"deleted": True, "id": memory_id})
