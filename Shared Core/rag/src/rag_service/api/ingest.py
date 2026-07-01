"""Document ingestion (Component 2 + 5) — inline, presigned upload, finalize, delete.

Endpoints (all under /v1/kbs/{kb_id}):
  * POST  /documents                 — inline ingest (≤ inline_max_bytes); SYNC chunk+embed+store.
  * POST  /documents/upload-url      — presigned PUT URL (size/content-type validated first).
  * POST  /documents/finalize        — HeadObject the key + enqueue ingestion via the outbox
                                       (Idempotency-Key MANDATORY semantics; replay on hit).
  * GET   /documents                 — list (paginated).
  * GET   /documents/{doc_id}        — status.
  * DELETE /documents/{doc_id}       — DB cascade + s3_deletions queue row (async sweeper).

ACL is enforced on every call (rag:ingest for writes, rag:query for reads → OP_*). Quotas:
documents_per_kb_max + storage_bytes_max (413) on the write paths.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..core import trace
from ..core.auth import SCOPE_INGEST, SCOPE_QUERY, Principal, require_scope
from ..core.errors import ApiError, ErrorCode, parse_uuid
from ..db import outbox, repository
from ..db.pool import in_tenant
from ..models.api import (
    DocumentListResponse,
    DocumentResponse,
    FinalizeRequest,
    InlineIngestRequest,
    UploadUrlRequest,
    UploadUrlResponse,
)
from ..services import acl, idempotency, quota
from ..services import ingest as ingest_pipeline
from ..services.acl import OP_INGEST, OP_QUERY
from ..services.object_store import build_source_uri, object_key, sanitize_filename
from ..services.store import resolve_vector_store

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/kbs", tags=["documents"])


def _require_pool(request: Request) -> object:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Database is not available.")
    return pool


def _agent_jwt(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-agent-jwt")
    if fwd:
        return fwd
    auth = request.headers.get("authorization", "")
    return auth.partition(" ")[2].strip() or None


async def _load_kb(pool: object, principal: Principal, kb_id: str) -> dict:
    kb = await repository.get_kb(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
    if kb is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Knowledge base not found.")
    return kb


# ── Inline ingest ───────────────────────────────────────────────────────────────
@router.post("/{kb_id}/documents", response_model=None, status_code=201)
async def inline_ingest(
    kb_id: str,
    body: InlineIngestRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_INGEST)),
) -> DocumentResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    settings = request.app.state.settings
    pool = _require_pool(request)
    kb = await _load_kb(pool, principal, kb_id)
    await acl.check_access(pool, principal, kb_id, OP_INGEST, settings=settings)

    content_bytes = body.content.encode("utf-8")
    if len(content_bytes) > settings.inline_max_bytes:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Inline content is {len(content_bytes)} bytes; the maximum is {settings.inline_max_bytes}.",
            details={"reason": "INLINE_TOO_LARGE", "max_bytes": settings.inline_max_bytes},
        )

    # Quotas: doc count + storage bytes (413 over).
    await quota.enforce_documents_per_kb_max(pool, principal, kb_id, settings=settings)  # type: ignore[arg-type]
    await quota.enforce_storage_bytes_max(
        pool, principal, additional_bytes=len(content_bytes), settings=settings  # type: ignore[arg-type]
    )

    doc_id = str(uuid.uuid4())
    key = object_key(principal.tenant_id, doc_id, body.name)
    source_uri = build_source_uri(settings.s3_bucket, key)

    # Persist content to object storage (degrades to no-op without an endpoint).
    object_store = request.app.state.object_store
    await object_store.put_object(key, content_bytes, content_type="text/plain")

    doc = await repository.create_document(
        pool,  # type: ignore[arg-type]
        principal.tenant_id,
        kb_id=kb_id,
        name=body.name,
        source_type=body.source_type,
        source_uri=source_uri,
        status="processing",
        metadata=body.metadata,
        doc_id=doc_id,
    )

    # SYNCHRONOUS chunk + embed + store (small payload).
    embedder = request.app.state.embedder
    store = await resolve_vector_store(pool, settings, principal.tenant_id)
    result = await ingest_pipeline.ingest_text(
        text=body.content,
        doc_id=doc_id,
        kb_id=kb_id,
        tenant_id=principal.tenant_id,
        embedding_model=kb["embedding_model_resolved"],
        embedding_dim=kb["embedding_dim"],
        chunking_strategy=kb["chunking_strategy"],
        chunk_size=kb["chunk_size"],
        chunk_overlap=kb["chunk_overlap"],
        doc_name=body.name,
        source_uri=source_uri,
        embedder=embedder,
        store=store,
        settings=settings,
        agent_jwt=_agent_jwt(request),
        on_behalf_of=principal.agent_id or principal.on_behalf_of,
        contextualizer=getattr(request.app.state, "contextualizer", None),
        doc_metadata=body.metadata,
    )

    # Mark complete + emit completed event + usage in the same txn.
    await _complete_document(
        pool, principal, doc_id, kb_id, result.chunks_indexed, result.embedding_tokens_used,
        storage_bytes=len(content_bytes), settings=settings,
    )

    from ..core import metrics

    metrics.ingest_total.labels("inline").inc()
    metrics.chunks_indexed_total.inc(result.chunks_indexed)

    doc = await repository.get_document(pool, principal.tenant_id, doc_id)  # type: ignore[arg-type]
    return DocumentResponse(**doc)  # type: ignore[arg-type]


# ── Presigned upload-url ──────────────────────────────────────────────────────────
@router.post("/{kb_id}/documents/upload-url", response_model=None)
async def upload_url(
    kb_id: str,
    body: UploadUrlRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_INGEST)),
) -> UploadUrlResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    settings = request.app.state.settings
    pool = _require_pool(request)
    await _load_kb(pool, principal, kb_id)
    await acl.check_access(pool, principal, kb_id, OP_INGEST, settings=settings)

    if body.size_bytes > settings.upload_max_bytes:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"size_bytes {body.size_bytes} exceeds the cap of {settings.upload_max_bytes}.",
            details={"reason": "UPLOAD_TOO_LARGE", "max_bytes": settings.upload_max_bytes},
        )
    allow = {c.strip() for c in settings.upload_content_type_allowlist.split(",") if c.strip()}
    if body.content_type not in allow:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"content_type '{body.content_type}' is not allowed.",
            details={"reason": "CONTENT_TYPE_NOT_ALLOWED", "allowed": sorted(allow)},
        )
    await quota.enforce_documents_per_kb_max(pool, principal, kb_id, settings=settings)  # type: ignore[arg-type]

    doc_id = str(uuid.uuid4())
    key = object_key(principal.tenant_id, doc_id, body.filename)
    object_store = request.app.state.object_store
    url = object_store.presign_put(key, content_type=body.content_type)

    # Create a pending document row so finalize can validate + transition it.
    await repository.create_document(
        pool,  # type: ignore[arg-type]
        principal.tenant_id,
        kb_id=kb_id,
        name=sanitize_filename(body.filename),
        source_type=_source_type_for(body.content_type),
        source_uri=build_source_uri(settings.s3_bucket, key),
        status="pending",
        doc_id=doc_id,
    )
    return UploadUrlResponse(
        upload_url=url, doc_id=doc_id, expires_in=settings.presign_expiry_seconds
    )


def _source_type_for(content_type: str) -> str:
    return {
        "application/pdf": "pdf",
        "text/markdown": "markdown",
        "text/plain": "text",
    }.get(content_type, "text")


# ── Finalize (idempotent; enqueues the worker via the outbox) ─────────────────────
@router.post("/{kb_id}/documents/finalize", response_model=None)
async def finalize(
    kb_id: str,
    body: FinalizeRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_INGEST)),
) -> JSONResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    doc_id = parse_uuid(body.doc_id, field="doc_id")
    settings = request.app.state.settings
    pool = _require_pool(request)
    valkey = getattr(request.app.state, "valkey", None)
    kb = await _load_kb(pool, principal, kb_id)
    await acl.check_access(pool, principal, kb_id, OP_INGEST, settings=settings)

    idem_key = request.headers.get("Idempotency-Key")
    claimed = False
    if idem_key:
        state = await idempotency.begin(valkey, idem_key, principal, kb_id, settings=settings)
        if state is idempotency.BeginState.IN_FLIGHT:
            idempotency.raise_in_flight()  # 409
        if state is idempotency.BeginState.COMPLETED:
            replay = await idempotency.get_replay(
                valkey, idem_key, principal, kb_id, settings=settings
            )
            if replay is not None:
                from ..core import metrics

                metrics.ingest_dedup_total.labels("idempotency_replay").inc()
                return JSONResponse(content=replay, headers={idempotency.REPLAY_HEADER: "true"})
        # We hold a fresh in_flight slot — it MUST be released on any failure below (retryable
        # validation / enqueue errors) so a client retry is not blocked for the in-flight TTL.
        claimed = state is idempotency.BeginState.NEW

    # Everything past the claim must release the in_flight slot on failure (BUG 3): the slot is
    # only converted to a durable 'completed' record after the outbox txn commits.
    try:
        doc = await repository.get_document(pool, principal.tenant_id, doc_id)  # type: ignore[arg-type]
        if doc is None or doc["kb_id"] != kb_id:
            raise ApiError(ErrorCode.NOT_FOUND, "Document not found for this knowledge base.")

        # HeadObject the expected key (degrades to present without a live endpoint).
        object_store = request.app.state.object_store
        key = doc["source_uri"].split(f"{settings.s3_bucket}/", 1)[-1] if doc["source_uri"] else ""
        if key and not key.startswith(f"{principal.tenant_id}/"):
            raise ApiError(ErrorCode.FORBIDDEN, "Object key tenant prefix mismatch.")
        head = await object_store.head_object(key)
        if not head.exists:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Uploaded object not found at the expected key.",
                details={"reason": "OBJECT_NOT_FOUND"},
            )

        # Enqueue the ingestion work-order via the outbox (self-contained payload — the worker
        # does NOT re-read KB config) in the same txn as the status transition to 'processing'.
        request_id = trace.request_id_var.get()
        trace_id = trace.trace_id_var.get()
        payload = {
            "doc_id": doc_id,
            "kb_id": kb_id,
            "tenant_id": principal.tenant_id,
            "source_uri": doc["source_uri"],
            "source_type": doc["source_type"],
            "embedding_model_resolved": kb["embedding_model_resolved"],
            "embedding_dim": kb["embedding_dim"],
            "chunking_strategy": kb["chunking_strategy"],
            "chunk_size": kb["chunk_size"],
            "chunk_overlap": kb["chunk_overlap"],
            "agent_id": principal.agent_id,
            "request_id": request_id,
            "trace_id": trace_id,
        }

        async def _txn(conn: object) -> None:
            await conn.execute(  # type: ignore[attr-defined]
                "UPDATE rag.documents SET status = 'pending' WHERE doc_id = %s", (doc_id,)
            )
            await outbox.enqueue_outbox(
                conn, outbox.TOPIC_INGESTION_REQUESTED, principal.tenant_id, trace_id, payload,  # type: ignore[arg-type]
                producer_version=settings.service_version,
            )

        await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
    except Exception:
        # Release the (retryable) in_flight claim so the same Idempotency-Key can retry now
        # instead of 409'ing for the full TTL. complete() below converts it to 'completed' on
        # success; release() only deletes a still-in_flight slot (never a completed record).
        if claimed and idem_key:
            await idempotency.release(valkey, idem_key, principal, kb_id, settings=settings)
        raise

    from ..core import metrics

    metrics.ingest_total.labels("finalize").inc()

    response_body = {"doc_id": doc_id, "status": "pending"}
    if idem_key:
        await idempotency.complete(valkey, idem_key, principal, kb_id, response_body, settings=settings)
    return JSONResponse(content=response_body, status_code=202)


# ── Document list / status / delete ───────────────────────────────────────────────
@router.get("/{kb_id}/documents", response_model=None)
async def list_documents(
    kb_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_QUERY, SCOPE_INGEST)),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentListResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    settings = request.app.state.settings
    pool = _require_pool(request)
    await _load_kb(pool, principal, kb_id)
    await acl.check_access(pool, principal, kb_id, OP_QUERY, settings=settings)
    rows = await repository.list_documents(
        pool, principal.tenant_id, kb_id, limit=limit, offset=offset  # type: ignore[arg-type]
    )
    next_offset = offset + limit if len(rows) == limit else None
    return DocumentListResponse(
        documents=[DocumentResponse(**r) for r in rows], next_offset=next_offset
    )


@router.get("/{kb_id}/documents/{doc_id}", response_model=None)
async def document_status(
    kb_id: str,
    doc_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_QUERY, SCOPE_INGEST)),
) -> DocumentResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    doc_id = parse_uuid(doc_id, field="doc_id")
    settings = request.app.state.settings
    pool = _require_pool(request)
    # Load the KB first so a missing KB is 404 (not a 403 from the ACL check) — parity with
    # the query handler (MINOR fix).
    await _load_kb(pool, principal, kb_id)
    await acl.check_access(pool, principal, kb_id, OP_QUERY, settings=settings)
    doc = await repository.get_document(pool, principal.tenant_id, doc_id)  # type: ignore[arg-type]
    if doc is None or doc["kb_id"] != kb_id:
        raise ApiError(ErrorCode.NOT_FOUND, "Document not found.")
    return DocumentResponse(**doc)


@router.delete("/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_document(
    kb_id: str,
    doc_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_INGEST)),
) -> None:
    kb_id = parse_uuid(kb_id, field="kb_id")
    doc_id = parse_uuid(doc_id, field="doc_id")
    settings = request.app.state.settings
    pool = _require_pool(request)
    # Load the KB first so a missing KB is 404 (not a 403 from the ACL check) — parity with
    # the query handler (MINOR fix).
    await _load_kb(pool, principal, kb_id)
    await acl.check_access(pool, principal, kb_id, OP_INGEST, settings=settings)
    doc = await repository.get_document(pool, principal.tenant_id, doc_id)  # type: ignore[arg-type]
    if doc is None or doc["kb_id"] != kb_id:
        raise ApiError(ErrorCode.NOT_FOUND, "Document not found.")

    s3_prefix = f"{principal.tenant_id}/{doc_id}/"

    async def _txn(conn: object) -> None:
        # Cascade: chunks + chunk_vectors_* drop ON DELETE CASCADE off documents.
        await conn.execute("DELETE FROM rag.documents WHERE doc_id = %s", (doc_id,))  # type: ignore[attr-defined]
        # Durable S3-delete handoff (Component 5 — the queue, not an inline S3 call).
        await conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO rag.s3_deletions (doc_id, tenant_id, s3_prefix)
            VALUES (%s, %s, %s)
            ON CONFLICT (doc_id) DO NOTHING
            """,
            (doc_id, principal.tenant_id, s3_prefix),
        )

    await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
    logger.info("document_deleted", doc_id=doc_id, kb_id=kb_id, tenant=principal.tenant_id)


# ── Shared completion helper ──────────────────────────────────────────────────────
async def _complete_document(
    pool: object,
    principal: Principal,
    doc_id: str,
    kb_id: str,
    chunks_indexed: int,
    embedding_tokens: int,
    *,
    storage_bytes: int,
    settings: object,
) -> None:
    """Mark a document complete + emit ingestion.completed + usage in one txn."""
    request_id = trace.request_id_var.get()
    trace_id = trace.trace_id_var.get()
    completed_payload = {
        "doc_id": doc_id,
        "kb_id": kb_id,
        "tenant_id": principal.tenant_id,
        "chunk_count": chunks_indexed,
        "request_id": request_id,
        "trace_id": trace_id,
    }
    usage_payload = {
        "tenant_id": principal.tenant_id,
        "agent_id": principal.agent_id,
        "api_key_id": principal.api_key_id,
        "operation": "rag.ingest",
        "units": {
            "chunks_indexed": chunks_indexed,
            "embedding_tokens_used": embedding_tokens,
            "storage_bytes_added": storage_bytes,
        },
        "request_id": request_id,
        "trace_id": trace_id,
    }

    async def _txn(conn: object) -> None:
        await conn.execute(  # type: ignore[attr-defined]
            "UPDATE rag.documents SET status = 'completed', completed_at = NOW() WHERE doc_id = %s",
            (doc_id,),
        )
        await outbox.enqueue_outbox(
            conn, outbox.TOPIC_INGESTION_COMPLETED, principal.tenant_id, trace_id,  # type: ignore[arg-type]
            completed_payload, producer_version=settings.service_version,  # type: ignore[attr-defined]
        )
        await outbox.enqueue_outbox(
            conn, outbox.TOPIC_USAGE_RECORDED, principal.tenant_id, trace_id,  # type: ignore[arg-type]
            usage_payload, producer_version=settings.service_version,  # type: ignore[attr-defined]
        )

    await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
