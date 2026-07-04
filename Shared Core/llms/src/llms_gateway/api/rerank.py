"""POST /v1/rerank — cross-encoder reranking surface (RAG dependency).

Pluggable provider whose DEFAULT is a deterministic MOCK (keyless, offline-stable),
mirroring how ``/v1/embeddings`` selects mock vs real and following the same
non-streaming request lifecycle as ``api/embeddings.py``:

    auth dependency -> validate -> resolve model (alias 'rerank-default' default) ->
    document/payload caps (413) -> per-key ACL (Contract-18) -> idempotency begin
    (Contract-9) -> rate-limit pre-gate -> provider.rerank -> metering: debit + write
    usage_records + outbox in a tenant transaction (fail-open billing, UNITS not a cost
    rewrite) -> return the unified {results, model, usage} response.

Provider selection (``RERANK_PROVIDER``, default 'mock'): the deterministic lexical
reranker (no keys / no network / no heavy deps) by default; the local cross-encoder
seam (bge-reranker class) behind ``RERANK_PROVIDER=local`` — NOT in the default image.

Caps (config keys, env-overridable):
  * ``rerank_max_documents``     — max candidate documents in one request.
  * ``rerank_max_payload_bytes`` — max total UTF-8 byte size of query + all doc text.
Over either cap -> 413 ``VALIDATION_ERROR`` (Contract-2) BEFORE the provider runs.

Billing-write failures after the provider already ran never 5xx the client: logged +
counted, the UsageWrite is journalled for a later replay worker, and the response is
served with ``X-Cypherx-Billing-Pending: true`` — identical to chat/embeddings.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core import metrics, trace
from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..db.outbox import UsageWrite, record_usage
from ..models.unified import RerankRequest, RerankResponse
from ..services import billing_journal, idempotency, rate_limit
from ..services.acl import enforce_acl
from ..services.auth_client import resolve_limits
from ..services.idempotency import BeginState
from ..services.providers.rerank import get_rerank_provider
from ..services.router import ModelRouter, Resolution

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["rerank"])

_OPERATION = "rerank"


def _get_router(request: Request) -> ModelRouter:
    return request.app.state.model_router


def _enforce_input_caps(request: Request, body: RerankRequest) -> None:
    """Reject oversized rerank batches BEFORE the provider call (413).

    Two ceilings (config keys): document count and total UTF-8 payload bytes (query +
    every document text). Over either -> Contract-2 413 ``VALIDATION_ERROR``.
    """
    settings = request.app.state.settings

    max_docs = settings.rerank_max_documents
    if len(body.documents) > max_docs:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"documents has {len(body.documents)} items; the maximum is {max_docs}.",
            status_code=413,
            details={
                "reason": "DOCUMENTS_EXCEEDED",
                "documents": len(body.documents),
                "max_documents": max_docs,
            },
        )

    max_bytes = settings.rerank_max_payload_bytes
    total_bytes = len(body.query.encode("utf-8")) + sum(
        len(d.text.encode("utf-8")) for d in body.documents
    )
    if total_bytes > max_bytes:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"rerank payload is {total_bytes} bytes; the maximum is {max_bytes}.",
            status_code=413,
            details={
                "reason": "PAYLOAD_BYTES_EXCEEDED",
                "bytes": total_bytes,
                "max_bytes": max_bytes,
            },
        )


@router.post("/rerank", response_model=None)
async def rerank(
    body: RerankRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    settings = request.app.state.settings
    model_router = _get_router(request)
    # Resolve the alias/literal to a (provider, model_id). 'rerank-default' is the
    # platform default alias -> the cypherx mock reranker.
    requested_model = body.model or settings.rerank_default_model
    resolution = await model_router.resolve(requested_model, principal.tenant_id)

    valkey = getattr(request.app.state, "valkey", None)
    pool = getattr(request.app.state, "db_pool", None)

    # ── Per-key ACL (Contract-18) — AFTER auth + model resolution, BEFORE backend work.
    await enforce_acl(
        pool,
        principal,
        model=resolution.model_id,
        provider=resolution.provider,
        operation=_OPERATION,
        settings=settings,
    )

    # ── Input / payload caps (413 over a cap) — BEFORE any backend work ───────
    _enforce_input_caps(request, body)

    # ── Idempotency (Contract-9) — rerank is never streamed (full support) ────
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        state = await idempotency.begin(valkey, idem_key, principal, settings=settings)
        if state is BeginState.IN_FLIGHT:
            idempotency.raise_in_flight()  # 409
        if state is BeginState.COMPLETED:
            replay = await idempotency.get_replay(valkey, idem_key, principal, settings=settings)
            if replay is not None:
                return JSONResponse(
                    content=replay.body,
                    status_code=replay.status_code,
                    headers={idempotency.REPLAY_HEADER: "true"},
                )
            # COMPLETED but body unreadable (fail-open) -> fall through and recompute.

    # ── Rate limiting: pre-request gate (429 + Retry-After over limit) ────────
    limits = await resolve_limits(principal, pool=pool, settings=settings)
    await rate_limit.enforce_pre(valkey, principal, limits, settings=settings)

    # ── Provider: deterministic mock (default) or local cross-encoder seam ────
    provider = get_rerank_provider(settings)
    started = time.monotonic()
    response: RerankResponse = await provider.rerank(body, model_id=resolution.model_id)
    duration_ms = int((time.monotonic() - started) * 1000)

    u = response.usage
    metrics.requests_total.labels(resolution.provider, resolution.model_id, "success").inc()
    metrics.request_duration_seconds.labels(resolution.provider, resolution.model_id).observe(
        duration_ms / 1000
    )
    # Rerank meters processed tokens as the prompt direction (no completion tokens).
    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "prompt").inc(
        u.total_tokens
    )

    # Post-hoc token debit (best-effort; enforced on the NEXT request's enforce_pre).
    await rate_limit.debit_tokens(valkey, principal, u.total_tokens, 0, settings=settings)

    billing_pending = await _write_usage(request, principal, resolution, u, duration_ms)

    response_body = response.model_dump(by_alias=True)
    headers: dict[str, str] = {}
    if billing_pending:
        headers["X-Cypherx-Billing-Pending"] = "true"

    if idem_key:
        await idempotency.complete(
            valkey, idem_key, principal, 200, response_body, settings=settings
        )

    return JSONResponse(content=response_body, headers=headers or None)


async def _write_usage(
    request: Request,
    principal: Principal,
    resolution: Resolution,
    usage: object,
    duration_ms: int,
) -> bool:
    """Persist usage + outbox with operation="rerank". True if the write failed.

    Rerank has no token cost (cost_usd stays 0 — NO cost rewrite): metering is by UNITS
    (total_tokens processed + a request_id correlation), per Contract-19. On a DB
    failure the UsageWrite is journalled (fail-open) so a replay worker can re-drive it.
    Mirrors ``api/embeddings._write_usage``.
    """
    pool = getattr(request.app.state, "db_pool", None)
    settings = request.app.state.settings
    u = usage  # RerankUsage: total_tokens + search_units

    if pool is None:
        logger.warning("usage_write_skipped_no_pool")
        return False

    write = UsageWrite(
        llm_call_id=str(uuid.uuid4()),
        request_id=trace.request_id_var.get(),
        tenant_id=principal.tenant_id,
        trace_id=trace.trace_id_var.get(),
        provider=resolution.provider,
        model=resolution.model_id,
        prompt_tokens=u.total_tokens,  # type: ignore[attr-defined]
        completion_tokens=0,
        total_tokens=u.total_tokens,  # type: ignore[attr-defined]
        cost_usd=0.0,  # rerank meters UNITS, not token cost — no cost rewrite.
        duration_ms=duration_ms,
        agent_id=principal.agent_id,
        api_key_id=principal.api_key_id,
        principal_type=principal.principal_type,
        operation=_OPERATION,
    )
    try:
        await record_usage(pool, write, producer_version=settings.service_version)
        return False
    except Exception as exc:  # noqa: BLE001 — never 5xx after the provider ran
        logger.error("billing_write_failed", reason="db_unreachable", error=str(exc))
        metrics.billing_write_failed_total.labels("db_unreachable").inc()
        await billing_journal.append(write, settings=settings)
        return True
