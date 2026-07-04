"""POST /v1/embeddings — the WP06 embeddings surface (RAG + Memory dependency).

Mirrors the non-streaming chat lifecycle in ``api/chat.py`` minus streaming:

    auth dependency -> validate -> resolve model -> input/payload caps (413) ->
    idempotency begin (Contract-9) -> provider.embed -> compute cost (input tokens
    only, output 0) -> debit tokens + write usage_records + outbox in a tenant
    transaction (fail-open billing) -> return the unified response.

Billing-write failures after the provider already returned tokens never 5xx the
client (they already paid): the failure is logged + counted, the UsageWrite is
journalled for a later replay worker, and the response is served with
``X-Cypherx-Billing-Pending: true`` (Component 4) — identical to the chat path.

Caps (config keys, env-overridable):
  * ``embeddings_max_input_items``   — max strings in a list ``input``.
  * ``embeddings_max_payload_bytes`` — max total UTF-8 byte size of all input text.
Over either cap -> 413 ``VALIDATION_ERROR`` (Contract-2) BEFORE the provider runs.

Idempotency (Contract-9, full support — embeddings are never streamed):
  ``Idempotency-Key`` header -> ``begin``; IN_FLIGHT -> 409, COMPLETED -> replay with
  ``Idempotency-Replayed: true``, NEW/FAILOPEN -> proceed then ``complete``.
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
from ..models.unified import EmbeddingRequest, EmbeddingResponse
from ..services import billing_journal, idempotency, rate_limit
from ..services.acl import enforce_acl
from ..services.auth_client import resolve_limits
from ..services.cost import cost_calculator
from ..services.idempotency import BeginState
from ..services.router import ModelRouter, Resolution

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["embeddings"])

_OPERATION = "embedding"


def _get_router(request: Request) -> ModelRouter:
    return request.app.state.model_router


def _enforce_input_caps(request: Request, body: EmbeddingRequest) -> int:
    """Reject oversized embeddings batches BEFORE the provider call (413).

    Enforces two ceilings (config keys):
      * item count — only meaningful for a list ``input`` (a single string is 1 item);
      * total UTF-8 payload bytes across all input strings.
    Returns the item count (so the caller can record it / build the response). Raises a
    Contract-2 413 ``VALIDATION_ERROR`` with a clear ``reason`` when either is exceeded.
    """
    settings = request.app.state.settings
    items = [body.input] if isinstance(body.input, str) else body.input

    max_items = settings.embeddings_max_input_items
    if len(items) > max_items:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"input has {len(items)} items; the maximum is {max_items}.",
            status_code=413,
            details={
                "reason": "INPUT_ITEMS_EXCEEDED",
                "items": len(items),
                "max_items": max_items,
            },
        )

    max_bytes = settings.embeddings_max_payload_bytes
    total_bytes = sum(len(t.encode("utf-8")) for t in items)
    if total_bytes > max_bytes:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"input payload is {total_bytes} bytes; the maximum is {max_bytes}.",
            status_code=413,
            details={
                "reason": "PAYLOAD_BYTES_EXCEEDED",
                "bytes": total_bytes,
                "max_bytes": max_bytes,
            },
        )
    return len(items)


@router.post("/embeddings", response_model=None)
async def embeddings(
    body: EmbeddingRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    model_router = _get_router(request)
    resolution = await model_router.resolve(body.model, principal.tenant_id)
    # BYOK-aware: prefer the tenant's registered provider key (fail-open to platform key).
    provider = await model_router.provider_for_request(resolution, principal.tenant_id)

    settings = request.app.state.settings
    valkey = getattr(request.app.state, "valkey", None)
    pool = getattr(request.app.state, "db_pool", None)

    # ── Per-key ACL (Contract-18) — AFTER auth + model/provider resolution, BEFORE
    # any backend work. Fails OPEN (allow) with no DB pool / no ACL rows for the key. ─
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

    # ── Idempotency (Contract-9) — embeddings are never streamed (full support) ─
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

    started = time.monotonic()
    response: EmbeddingResponse = await provider.embed(body, model_id=resolution.model_id)  # type: ignore[attr-defined]
    duration_ms = int((time.monotonic() - started) * 1000)

    # Authoritative cost from input tokens only (output cost 0 by convention).
    u = response.usage
    u.cost_usd = cost_calculator.compute(
        resolution.provider,
        resolution.model_id,
        prompt_tokens=u.prompt_tokens,
        completion_tokens=0,
    )

    metrics.requests_total.labels(resolution.provider, resolution.model_id, "success").inc()
    metrics.request_duration_seconds.labels(resolution.provider, resolution.model_id).observe(
        duration_ms / 1000
    )
    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "prompt").inc(u.prompt_tokens)

    # Post-hoc token debit (best-effort; enforced on the NEXT request's enforce_pre).
    # Embeddings have no completion tokens.
    await rate_limit.debit_tokens(valkey, principal, u.prompt_tokens, 0, settings=settings)

    billing_pending = await _write_usage(request, principal, resolution, u, duration_ms)

    response_body = response.model_dump(by_alias=True)
    headers: dict[str, str] = {}
    if billing_pending:
        headers["X-Cypherx-Billing-Pending"] = "true"

    # Idempotency complete: store the finished response for future replay (no-op
    # without a key / Valkey / when disabled — fail-open).
    if idem_key:
        await idempotency.complete(valkey, idem_key, principal, 200, response_body, settings=settings)

    return JSONResponse(content=response_body, headers=headers or None)


async def _write_usage(
    request: Request,
    principal: Principal,
    resolution: Resolution,
    usage: object,
    duration_ms: int,
) -> bool:
    """Persist usage + outbox with operation="embedding". True if the write failed.

    On a DB failure the UsageWrite is appended to the billing-replay journal (best-effort,
    fail-open) so a replay worker can re-drive it once the DB recovers. Mirrors
    ``api/chat._write_usage`` (embeddings carry no completion / cache tokens).
    """
    pool = getattr(request.app.state, "db_pool", None)
    settings = request.app.state.settings
    u = usage  # typed loosely; has the EmbeddingUsage fields

    if pool is None:
        # Local/unit-test path: no DB configured. Do not 5xx; just skip persistence.
        logger.warning("usage_write_skipped_no_pool")
        return False

    write = UsageWrite(
        # Gateway-minted billing key — one fresh UUIDv4 per provider call.
        llm_call_id=str(uuid.uuid4()),
        request_id=trace.request_id_var.get(),
        tenant_id=principal.tenant_id,
        trace_id=trace.trace_id_var.get(),
        provider=resolution.provider,
        model=resolution.model_id,
        prompt_tokens=u.prompt_tokens,  # type: ignore[attr-defined]
        completion_tokens=0,
        total_tokens=u.total_tokens,  # type: ignore[attr-defined]
        cost_usd=u.cost_usd,  # type: ignore[attr-defined]
        duration_ms=duration_ms,
        agent_id=principal.agent_id,
        api_key_id=principal.api_key_id,
        principal_type=principal.principal_type,
        operation=_OPERATION,
    )
    try:
        await record_usage(pool, write, producer_version=settings.service_version)
        return False
    except Exception as exc:  # noqa: BLE001 — never 5xx after provider charged tokens
        logger.error("billing_write_failed", reason="db_unreachable", error=str(exc))
        metrics.billing_write_failed_total.labels("db_unreachable").inc()
        # Journal for a later replay worker (best-effort; does not block the response).
        await billing_journal.append(write, settings=settings)
        return True
