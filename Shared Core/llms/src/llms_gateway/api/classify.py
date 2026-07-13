"""POST /v1/classify — safety/moderation classifier surface.

Pluggable provider whose DEFAULT is the deterministic STUB (keyword/permissive,
keyless), honoring the platform-wide ``CLASSIFIER_MODE=stub`` default and mirroring how
``/v1/embeddings`` selects mock vs real. Same non-streaming request lifecycle as
``api/embeddings.py``:

    auth dependency -> validate -> resolve model (alias 'safety-default' default) ->
    payload-byte cap (413) -> per-key ACL (Contract-18) -> idempotency begin
    (Contract-9) -> rate-limit pre-gate -> provider.classify -> metering: debit + write
    usage_records + outbox in a tenant transaction (fail-open billing, UNITS not a cost
    rewrite) -> return the unified {verdict, categories, model} response.

Provider selection (``CLASSIFIER_MODE``, default 'stub'): the deterministic keyword
classifier (verdict=allow + empty/low scores for clean text — today's permissive
behaviour) by default; the local safety-model seam (Llama Guard / ShieldGemma class)
behind ``CLASSIFIER_MODE=local`` — NOT in the default image.

Cap (config key, env-overridable): ``classify_max_input_bytes`` — over -> 413
``VALIDATION_ERROR`` (Contract-2) BEFORE the provider runs.

Billing-write failures after the provider already ran never 5xx the client: logged +
counted, journalled for replay, served with ``X-Cypherx-Billing-Pending: true``.
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
from ..models.unified import ClassifyRequest, ClassifyResponse
from ..services import billing_journal, idempotency, rate_limit
from ..services.acl import enforce_acl
from ..services.auth_client import resolve_limits
from ..services.idempotency import BeginState
from ..services.providers.safety import get_safety_provider
from ..services.router import ModelRouter, Resolution

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["classify"])

_OPERATION = "classify"


def _get_router(request: Request) -> ModelRouter:
    return request.app.state.model_router


def _enforce_input_caps(request: Request, body: ClassifyRequest) -> int:
    """Reject an oversized classify payload BEFORE the provider call (413).

    Returns the input byte size (for usage/metering). Over the cap -> Contract-2 413
    ``VALIDATION_ERROR`` (reason ``PAYLOAD_BYTES_EXCEEDED``).
    """
    settings = request.app.state.settings
    total_bytes = len(body.input.encode("utf-8"))
    max_bytes = settings.classify_max_input_bytes
    if total_bytes > max_bytes:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"input is {total_bytes} bytes; the maximum is {max_bytes}.",
            status_code=413,
            details={
                "reason": "PAYLOAD_BYTES_EXCEEDED",
                "bytes": total_bytes,
                "max_bytes": max_bytes,
            },
        )
    return total_bytes


@router.post("/classify", response_model=None)
async def classify(
    body: ClassifyRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse:
    settings = request.app.state.settings
    model_router = _get_router(request)
    # 'safety-default' platform alias -> the cypherx stub classifier. The client may
    # omit `model` (not a contract field); the default alias is used then.
    requested_model = getattr(body, "model", None) or settings.classifier_default_model
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

    # ── Input cap (413 over the cap) — BEFORE any backend work ────────────────
    input_bytes = _enforce_input_caps(request, body)

    # ── Idempotency (Contract-9) — classify is never streamed (full support) ──
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

    # ── Provider: deterministic stub (default) or local safety-model seam ─────
    provider = get_safety_provider(settings)
    started = time.monotonic()
    response: ClassifyResponse = await provider.classify(body, model_id=resolution.model_id)
    duration_ms = int((time.monotonic() - started) * 1000)

    # Classify meters one classification as a single processed "token" unit (no token
    # cost — UNITS not a cost rewrite). ~4 chars/token estimate for the processed size.
    processed_tokens = max(1, input_bytes // 4)
    metrics.requests_total.labels(resolution.provider, resolution.model_id, "success").inc()
    metrics.request_duration_seconds.labels(resolution.provider, resolution.model_id).observe(
        duration_ms / 1000
    )
    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "prompt").inc(
        processed_tokens
    )

    await rate_limit.debit_tokens(valkey, principal, processed_tokens, 0, settings=settings)

    billing_pending = await _write_usage(
        request, principal, resolution, processed_tokens, duration_ms
    )

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
    processed_tokens: int,
    duration_ms: int,
) -> bool:
    """Persist usage + outbox with operation="classify". True if the write failed.

    Classify has no token cost (cost_usd stays 0 — NO cost rewrite): metering is by
    UNITS (processed tokens + a request_id correlation), per Contract-19. On a DB
    failure the UsageWrite is journalled (fail-open). Mirrors ``api/embeddings._write_usage``.
    """
    pool = getattr(request.app.state, "db_pool", None)
    settings = request.app.state.settings

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
        prompt_tokens=processed_tokens,
        completion_tokens=0,
        total_tokens=processed_tokens,
        cost_usd=0.0,  # classify meters UNITS, not token cost — no cost rewrite.
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
