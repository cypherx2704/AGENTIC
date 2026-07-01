"""POST /v1/chat/completions — the critical-path spine.

Flow: auth dependency -> validate -> resolve model -> rate-limit pre-check ->
idempotency begin (non-stream) -> max_tokens ceiling -> provider.chat (or an SSE
StreamingResponse when stream=true) -> compute cost -> debit tokens + write
usage_records + outbox in a tenant transaction -> return the unified response (or
SSE stream).

Billing-write failures after the provider already returned tokens never 5xx the
client (they already paid) — the failure is logged + counted, the UsageWrite is
journalled for a later replay worker, and the response is served with
``X-Cypherx-Billing-Pending: true`` (Component 4).

WP05 wiring around the provider call (all FAIL-OPEN when their backend is absent —
the mock-provider unit path with ``db_pool=None`` and no Valkey is unaffected):

* **Idempotency (Contract-9, non-stream only):** ``Idempotency-Key`` header ->
  ``begin``; IN_FLIGHT -> 409, COMPLETED -> replay with ``Idempotency-Replayed: true``,
  NEW/FAILOPEN -> proceed then ``complete``.
* **Rate limiting:** ``enforce_pre`` before the call (429 + Retry-After over limit);
  ``debit_tokens`` after (best-effort, both stream + non-stream).
* **max_tokens ceiling:** reject 400 ``MAX_TOKENS_EXCEEDED`` over the model cap, or
  clamp with ``X-Cypherx-Param-Clamped: max_tokens`` per config policy.
* **Streaming correctness:** tool-call aggregation (providers), finish_reason +
  cache-token normalization (providers), mid-stream-error / wall-clock-timeout /
  client-disconnect handling with best-effort billing of tokens-so-far (here).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core import metrics, trace
from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..db.outbox import UsageWrite, record_usage
from ..models.unified import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ImageUrlContent,
    Usage,
)
from ..services import (
    alias_service,
    billing_journal,
    idempotency,
    rate_limit,
    tool_emulation,
    user_llm_rules,
)
from ..services.acl import enforce_acl
from ..services.auth_client import resolve_limits
from ..services.capabilities import capability_registry
from ..services.cost import cost_calculator
from ..services.idempotency import BeginState
from ..services.image_fetch import fetch_image_as_data_uri
from ..services.router import ModelRouter, Resolution

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["chat"])


def _get_router(request: Request) -> ModelRouter:
    return request.app.state.model_router


def _enforce_max_tokens(
    request: Request,
    body: ChatCompletionRequest,
    resolution: Resolution,
) -> str | None:
    """Enforce the model's max-output cap against ``body.max_tokens``.

    Returns the value for the ``X-Cypherx-Param-Clamped`` header (``"max_tokens"``)
    when a soft clamp was applied, else ``None``. Raises 400 ``MAX_TOKENS_EXCEEDED``
    when the policy is "reject" and the request exceeds the cap. Fail-open: an unknown
    model (no capability row) has no enforceable cap, so we leave the value as-is.
    """
    if body.max_tokens is None:
        return None
    cap = capability_registry.get(resolution.model_id)
    if cap is None or cap.max_tokens_cap <= 0:
        return None
    if body.max_tokens <= cap.max_tokens_cap:
        return None

    settings = request.app.state.settings
    if settings.max_tokens_over_cap_policy == "clamp":
        body.max_tokens = cap.max_tokens_cap
        metrics.param_clamped_total.labels("max_tokens").inc()
        logger.info(
            "max_tokens_clamped",
            model=resolution.model_id,
            cap=cap.max_tokens_cap,
        )
        return "max_tokens"
    # Default policy: reject.
    metrics.max_tokens_rejected_total.inc()
    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        f"max_tokens ({body.max_tokens}) exceeds the maximum for model "
        f"'{resolution.model_id}' ({cap.max_tokens_cap}).",
        status_code=400,
        details={
            "reason": "MAX_TOKENS_EXCEEDED",
            "requested": body.max_tokens,
            "max_tokens_cap": cap.max_tokens_cap,
            "model": resolution.model_id,
        },
    )


def _iter_image_parts(body: ChatCompletionRequest) -> list[ImageUrlContent]:
    """Collect every ``image_url`` content part across all messages (multimodal)."""
    parts: list[ImageUrlContent] = []
    for msg in body.messages:
        if isinstance(msg.content, list):
            parts.extend(p for p in msg.content if isinstance(p, ImageUrlContent))
    return parts


def _inline_image_bytes(url: str) -> int:
    """Decoded byte size of an inline ``data:`` image URI; 0 for a non-inline (http) URL.

    A ``data:[<mime>][;base64],<payload>`` URI carries the image bytes IN the request
    body, so they count toward the inline-byte cap. Base64 expands ~4/3, so the decoded
    size is ~ ``len(payload) * 3 / 4`` (we subtract padding for accuracy without
    actually decoding the — already in-memory — payload). A non-data URL carries no
    bytes in the body, so it contributes 0.
    """
    if not url.startswith("data:"):
        return 0
    _, _, payload = url.partition(",")
    if ";base64" in url.split(",", 1)[0]:
        b64 = payload.strip()
        padding = b64.count("=")
        return max(0, (len(b64) * 3) // 4 - padding)
    # A non-base64 data URI is percent/raw text; its body length is the byte size.
    return len(payload.encode("utf-8"))


def _enforce_image_caps(request: Request, body: ChatCompletionRequest) -> None:
    """Reject multimodal requests over the image count / inline-byte caps (413).

    Counts ALL image_url parts against ``max_images_per_request`` and sums the decoded
    bytes of INLINE (data-URI) images against ``max_image_bytes``. Both BEFORE the
    provider call. A text-only request has no image parts, so this is a no-op for it.
    Raises a Contract-2 413 ``PAYLOAD_TOO_LARGE`` with a clear ``reason`` on a breach.
    """
    parts = _iter_image_parts(body)
    if not parts:
        return  # text-only path — untouched.

    settings = request.app.state.settings
    max_images = settings.max_images_per_request
    if len(parts) > max_images:
        metrics.payload_too_large_total.labels("image_count").inc()
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"request has {len(parts)} image parts; the maximum is {max_images}.",
            status_code=413,
            details={
                "reason": "IMAGE_COUNT_EXCEEDED",
                "images": len(parts),
                "max_images": max_images,
            },
        )

    max_bytes = settings.max_image_bytes
    total_inline = sum(_inline_image_bytes(p.image_url.url) for p in parts)
    if total_inline > max_bytes:
        metrics.payload_too_large_total.labels("image_bytes").inc()
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"inline image payload is {total_inline} bytes; the maximum is {max_bytes}.",
            status_code=413,
            details={
                "reason": "IMAGE_BYTES_EXCEEDED",
                "bytes": total_inline,
                "max_bytes": max_bytes,
            },
        )


async def _inline_remote_images(request: Request, body: ChatCompletionRequest) -> None:
    """When ``image_inline_required`` is True, download each remote image_url to a base64
    data URI via the SSRF-hardened fetcher (and re-check the inline-byte cap afterward).

    NO-OP when the flag is False (the default) — the fetcher is never invoked and the
    image_url parts are forwarded to the provider as-is (URL pass-through).
    """
    settings = request.app.state.settings
    if not settings.image_inline_required:
        return
    parts = _iter_image_parts(body)
    changed = False
    for part in parts:
        url = part.image_url.url
        if url.startswith("data:"):
            continue  # already inline
        part.image_url.url = await fetch_image_as_data_uri(url, settings=settings)
        changed = True
    # Re-validate the inline-byte cap now that remote images are inlined into the body.
    if changed:
        _enforce_image_caps(request, body)


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> JSONResponse | StreamingResponse:
    model_router = _get_router(request)
    resolution = model_router.resolve(body.model, principal.tenant_id)
    # BYOK-aware: prefer the tenant's registered provider key (fail-open to platform key).
    provider = await model_router.provider_for_request(resolution, principal.tenant_id)

    settings = request.app.state.settings
    valkey = getattr(request.app.state, "valkey", None)
    pool = getattr(request.app.state, "db_pool", None)

    # ── Orchestrator LLM governance (BEFORE any backend work) ─────────────────
    # 1) Per-agent allowlist: a sub-agent confined to specific aliases cannot use others (403).
    await alias_service.enforce_agent_alias(pool, principal.tenant_id, principal.agent_id, body.model)
    # 2) Tenant user-defined LLM rules (block / agent-access). Returns whether this call's usage
    #    is billing-exempt (a user-added model the tenant marked billing_bypass).
    billing_bypass = await user_llm_rules.check_rules(
        pool,
        principal.tenant_id,
        provider=resolution.provider,
        model_id=resolution.model_id,
        principal_type=principal.principal_type,
    )

    # ── Per-key ACL (Contract-18) — AFTER auth + model/provider resolution, BEFORE
    # any backend work. Fails OPEN (allow) with no DB pool / no ACL rows for the key. ─
    await enforce_acl(
        pool,
        principal,
        model=resolution.model_id,
        provider=resolution.provider,
        operation="chat",
        settings=settings,
    )

    # ── Multimodal image caps (413 over count / inline-byte cap) — text-only no-op ─
    _enforce_image_caps(request, body)
    # Optional SSRF-hardened inline fetch (no-op unless image_inline_required=True).
    await _inline_remote_images(request, body)

    # ── max_tokens ceiling (400 over cap, or clamp + header) ──────────────────
    clamped_param = _enforce_max_tokens(request, body, resolution)

    # ── Idempotency (Contract-9) — non-stream only; replay a completed twin ───
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key and not body.stream:
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

    if body.stream:
        return await _stream(
            request, body, principal, model_router, resolution, provider, billing_bypass
        )
    return await _non_stream(
        request, body, principal, resolution, provider, idem_key, clamped_param, billing_bypass
    )


async def _non_stream(
    request: Request,
    body: ChatCompletionRequest,
    principal: Principal,
    resolution: Resolution,
    provider: object,
    idem_key: str | None,
    clamped_param: str | None,
    billing_bypass: bool = False,
) -> JSONResponse:
    settings = request.app.state.settings
    # Tool-calling emulation (small/non-native models): transform tools[] into a prompt
    # protocol, call the provider as a plain chat, and parse tool_calls back out — so the
    # response is byte-shaped exactly like a native tool-calling completion downstream.
    emulate_tools = tool_emulation.should_emulate(body, resolution.model_id, settings)
    started = time.monotonic()
    if emulate_tools:
        response = await tool_emulation.run_emulated_chat(
            provider, body, model_id=resolution.model_id, settings=settings  # type: ignore[arg-type]
        )
    else:
        response = await provider.chat(body, model_id=resolution.model_id)  # type: ignore[attr-defined]
    duration_ms = int((time.monotonic() - started) * 1000)

    # Compute authoritative cost from the normalized token counts.
    u = response.usage
    u.cost_usd = cost_calculator.compute(
        resolution.provider,
        resolution.model_id,
        prompt_tokens=u.prompt_tokens,
        completion_tokens=u.completion_tokens,
        cached_prompt_tokens=u.cached_prompt_tokens,
        cache_creation_tokens=u.cache_creation_tokens,
    )

    metrics.requests_total.labels(resolution.provider, resolution.model_id, "success").inc()
    metrics.request_duration_seconds.labels(resolution.provider, resolution.model_id).observe(
        duration_ms / 1000
    )
    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "prompt").inc(u.prompt_tokens)
    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "completion").inc(
        u.completion_tokens
    )

    # Post-hoc token debit (best-effort; enforced on the NEXT request's enforce_pre).
    settings = request.app.state.settings
    valkey = getattr(request.app.state, "valkey", None)
    await rate_limit.debit_tokens(
        valkey, principal, u.prompt_tokens, u.completion_tokens, settings=settings
    )

    # billing_bypass (a user-added model the tenant marked exempt): do NOT write usage_records
    # and do NOT emit the billing/usage Kafka events — the call is unmetered. Token rate-limit
    # debit still ran above (a bypass affects COST, not capacity accounting).
    billing_pending = False
    if not billing_bypass:
        billing_pending = await _write_usage(request, principal, resolution, u, duration_ms)

    response_body = response.model_dump(by_alias=True)
    headers: dict[str, str] = {}
    if billing_bypass:
        headers["X-Cypherx-Billing-Bypassed"] = "true"
    if billing_pending:
        headers["X-Cypherx-Billing-Pending"] = "true"
    if clamped_param:
        headers["X-Cypherx-Param-Clamped"] = clamped_param
    if body.tools:
        headers["X-Cypherx-Tool-Mode"] = "emulated" if emulate_tools else "native"

    # Idempotency complete: store the finished response for future replay (no-op
    # without a key / Valkey / when disabled — fail-open).
    if idem_key:
        await idempotency.complete(valkey, idem_key, principal, 200, response_body, settings=settings)

    return JSONResponse(content=response_body, headers=headers or None)


async def _stream(
    request: Request,
    body: ChatCompletionRequest,
    principal: Principal,
    model_router: ModelRouter,
    resolution: Resolution,
    provider: object,
    billing_bypass: bool = False,
) -> StreamingResponse:
    started = time.monotonic()
    settings = request.app.state.settings
    valkey = getattr(request.app.state, "valkey", None)
    timeout_s = settings.stream_wall_clock_timeout_seconds
    emulate_tools = tool_emulation.should_emulate(body, resolution.model_id, settings)

    async def event_source() -> AsyncIterator[bytes]:
        last_usage: dict | None = None
        terminal_reason: str | None = None  # provider_error | timeout | client_disconnect
        if emulate_tools:
            # Emulation buffers a non-stream call internally then emits consolidated SSE.
            source = tool_emulation.emulated_chat_stream(
                provider, body, model_id=resolution.model_id, settings=settings  # type: ignore[arg-type]
            )
        else:
            source = provider.chat_stream(body, model_id=resolution.model_id)  # type: ignore[attr-defined]
        deadline = started + timeout_s

        try:
            while True:
                # Client-disconnect cancellation: stop consuming, bill tokens-so-far.
                if await request.is_disconnected():
                    terminal_reason = "client_disconnect"
                    break
                # 120s wall-clock timeout (config key): bound a single chunk pull by the
                # time left on the budget so a hung upstream can't hold the SSE open.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminal_reason = "timeout"
                    break
                try:
                    chunk = await asyncio.wait_for(source.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    terminal_reason = "timeout"
                    break

                # Track the final usage chunk so we can persist a usage record afterwards.
                if chunk.startswith("data: ") and '"usage"' in chunk:
                    try:
                        parsed = json.loads(chunk[len("data: ") :].strip())
                        if isinstance(parsed, dict) and parsed.get("usage"):
                            last_usage = parsed["usage"]
                    except (ValueError, KeyError):
                        pass
                yield chunk.encode("utf-8")
        except asyncio.CancelledError:
            # Downstream cancelled the response generator (client gone / server shutdown).
            terminal_reason = "client_disconnect"
            await _close_stream(source)
            await _finalize_stream(
                request, principal, resolution, last_usage, started, valkey, settings, billing_bypass
            )
            raise
        except Exception as exc:  # noqa: BLE001 — mid-stream provider/transport error
            logger.warning("stream_consume_failed", error=str(exc))
            terminal_reason = "provider_error"

        # Stop consuming the provider on any abnormal exit (timeout/disconnect/error).
        if terminal_reason is not None:
            await _close_stream(source)
            metrics.stream_terminated_total.labels(terminal_reason).inc()
            # Emit a terminal SSE error event for timeout/provider_error (the client is
            # still connected). On client_disconnect there's no one to send to.
            if terminal_reason == "timeout":
                yield _sse_error(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    f"Stream exceeded the {timeout_s:.0f}s wall-clock limit.",
                )
            elif terminal_reason == "provider_error":
                yield _sse_error(ErrorCode.SERVICE_UNAVAILABLE, "Upstream stream failed.")

        # Best-effort billing of tokens burned so far (fail-open) — runs for BOTH the
        # normal completion and every abnormal exit.
        await _finalize_stream(
            request, principal, resolution, last_usage, started, valkey, settings, billing_bypass
        )

    stream_headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    if body.tools:
        stream_headers["X-Cypherx-Tool-Mode"] = "emulated" if emulate_tools else "native"
    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=stream_headers,
    )


async def _close_stream(source: AsyncIterator[str]) -> None:
    """Best-effort close of the provider async generator so it stops producing."""
    aclose = getattr(source, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:  # noqa: BLE001 — close must never raise into the response
        logger.warning("stream_provider_close_failed", error=str(exc))


def _sse_error(code: str, message: str) -> bytes:
    """A terminal SSE ``event: error`` frame (matches the providers' mid-stream shape)."""
    err = {"error": {"code": code, "message": message}}
    return f"event: error\ndata: {json.dumps(err)}\n\n".encode()


async def _finalize_stream(
    request: Request,
    principal: Principal,
    resolution: Resolution,
    last_usage: dict | None,
    started: float,
    valkey: object,
    settings: object,
    billing_bypass: bool = False,
) -> None:
    """Bill the tokens burned in a stream (debit + usage write). Always fail-open.

    Called exactly once per stream — on normal completion AND on every abnormal exit
    (timeout, disconnect, provider error). When no usage chunk was seen (the stream
    died before the terminal usage event) there is nothing to bill. When ``billing_bypass``
    is set (a user-added, unmetered model) the capacity debit still runs but the usage row /
    billing events are skipped.
    """
    if last_usage is None:
        return
    duration_ms = int((time.monotonic() - started) * 1000)
    usage = Usage(**{k: last_usage[k] for k in last_usage if k in Usage.model_fields})

    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "prompt").inc(
        usage.prompt_tokens
    )
    metrics.tokens_total.labels(resolution.provider, resolution.model_id, "completion").inc(
        usage.completion_tokens
    )

    await rate_limit.debit_tokens(
        valkey, principal, usage.prompt_tokens, usage.completion_tokens, settings=settings  # type: ignore[arg-type]
    )
    if not billing_bypass:
        await _write_usage(request, principal, resolution, usage, duration_ms)


async def _write_usage(
    request: Request,
    principal: Principal,
    resolution: Resolution,
    usage: object,
    duration_ms: int,
) -> bool:
    """Persist usage + outbox. Returns True if the write failed (billing pending).

    On a DB failure the UsageWrite is appended to the billing-replay journal (best-effort,
    fail-open) so a replay worker can re-drive it once the DB recovers.
    """
    pool = getattr(request.app.state, "db_pool", None)
    settings = request.app.state.settings
    u = usage  # typed loosely; has the Usage fields

    if pool is None:
        # Local/unit-test path: no DB configured. Do not 5xx; just skip persistence.
        logger.warning("usage_write_skipped_no_pool")
        return False

    write = UsageWrite(
        # Gateway-minted billing key — one fresh UUIDv4 per provider call (amended
        # fix #3). request_id below is correlation-only: two completions under one
        # forwarded X-Request-ID must BOTH bill.
        llm_call_id=str(uuid.uuid4()),
        request_id=trace.request_id_var.get(),
        tenant_id=principal.tenant_id,
        trace_id=trace.trace_id_var.get(),
        provider=resolution.provider,
        model=resolution.model_id,
        prompt_tokens=u.prompt_tokens,  # type: ignore[attr-defined]
        completion_tokens=u.completion_tokens,  # type: ignore[attr-defined]
        total_tokens=u.total_tokens,  # type: ignore[attr-defined]
        cached_prompt_tokens=u.cached_prompt_tokens,  # type: ignore[attr-defined]
        cache_creation_tokens=u.cache_creation_tokens,  # type: ignore[attr-defined]
        cost_usd=u.cost_usd,  # type: ignore[attr-defined]
        duration_ms=duration_ms,
        agent_id=principal.agent_id,
        api_key_id=principal.api_key_id,
        principal_type=principal.principal_type,
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
