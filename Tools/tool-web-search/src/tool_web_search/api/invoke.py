"""POST /mcp/v1/invoke — Contract-4 tool invocation.

Pipeline (in order):

1.  **Auth** — ``require_principal`` verifies the JWT (dual-mode) + the WP03 revocation
    mirror, and requires the coarse ``tool:invoke`` scope (403 otherwise).
2.  **Dual scope check** — the caller must ALSO hold ``tool:tool-web-search:invoke``
    (Contract-4 fine-grained scope) -> 403 FORBIDDEN otherwise.
3.  **Idempotency replay** — a repeated ``Idempotency-Key`` (per tenant) replays the
    stored result with header ``Idempotency-Replayed: true`` (Valkey-backed, fail-open).
4.  **Rate limit** — per-tenant Valkey fixed window; over limit -> 429 + ``Retry-After``
    (fail-open: Valkey absent/error -> allow).
5.  **input_schema validation** — invoke ``args`` validated against the manifest's JSON
    Schema; on failure -> 422 with a JSON Pointer to the offending field (e.g. ``/query``).
6.  **Provider call** — the env-selected provider (``mock`` | ``serpapi`` | ``brave``)
    runs the search (bounded by ``tool_timeout_seconds``).
7.  **10 MiB output cap** — if the serialized result exceeds the cap it is REJECTED with
    413 PAYLOAD_TOO_LARGE (never streamed back).
8.  **Store** — the result is cached under the Idempotency-Key for future replay.

Request body (Contract-4 invoke envelope)::

    { "tool": "web_search", "args": { "query": "...", "max_results": 5 } }

``tool`` is optional (this server exposes a single tool); when present it must equal
``web_search``. ``arguments`` is accepted as an alias for ``args``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, Response

from ..core import metrics
from ..core.auth import Principal, require_principal
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..services import idempotency, rate_limit
from ..services import manifest as manifest_svc
from ..services.providers import ProviderError, get_provider

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp"])


def _extract_args(payload: Any) -> dict[str, Any]:
    """Pull the invoke args out of the request body (``args`` or ``arguments``).

    Validates the envelope shape and the optional ``tool`` selector; raises a Contract-2
    422 VALIDATION_ERROR on a malformed envelope (NOT a schema violation — that is the
    args' own validation, handled separately with a JSON Pointer).
    """
    if not isinstance(payload, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Request body must be a JSON object.", status_code=422)

    tool = payload.get("tool")
    if tool is not None and tool != manifest_svc.TOOL_NAME:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"Unknown tool {tool!r}; this server exposes '{manifest_svc.TOOL_NAME}'.",
        )

    args = payload.get("args")
    if args is None:
        args = payload.get("arguments")
    if args is None:
        # No args wrapper at all: treat the whole body (minus the envelope keys) as args.
        args = {k: v for k, v in payload.items() if k not in {"tool", "args", "arguments"}}
    if not isinstance(args, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Invoke 'args' must be a JSON object.", status_code=422)
    return args


@router.post("/mcp/v1/invoke")
async def invoke(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Response:
    settings = get_settings()
    provider = get_provider(settings)

    # ── (2) Dual scope check (Contract-4): require BOTH scopes ──────────────────
    if not principal.has_scope(manifest_svc.FINE_SCOPE):
        metrics.invoke_rejected_total.labels("scope_denied").inc()
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"Token missing required scope '{manifest_svc.FINE_SCOPE}'.",
        )

    valkey = getattr(request.app.state, "valkey", None)
    idem_key = request.headers.get("idempotency-key")

    # ── (3) Idempotency replay ──────────────────────────────────────────────────
    replay = await idempotency.get_replay(valkey, idem_key, principal, settings=settings)
    if replay is not None:
        logger.info("invoke_replayed", tenant_id=principal.tenant_id)
        return _json_response(
            replay.body,
            replay.status_code,
            extra_headers={idempotency.REPLAY_HEADER: "true"},
        )

    # ── (4) Rate limit (fail-open) ──────────────────────────────────────────────
    await rate_limit.enforce(valkey, principal, settings=settings)

    # ── parse + (5) input_schema validation (422 + JSON Pointer) ────────────────
    payload = await _read_json(request)
    args = _extract_args(payload)
    try:
        manifest_svc.validate_input(args, settings)
    except manifest_svc.SchemaViolation as exc:
        metrics.invoke_rejected_total.labels("schema_invalid").inc()
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Input schema validation failed: {exc.message}",
            status_code=422,
            details={"pointer": exc.pointer, "reason": exc.message},
        ) from exc

    query: str = args["query"]
    max_results = int(args.get("max_results", settings.default_max_results))

    # ── (6) Provider call (bounded by the tool timeout) ─────────────────────────
    with metrics.invoke_duration_seconds.labels(provider.name).time():
        try:
            results = await asyncio.wait_for(
                provider.search(query, max_results),
                timeout=settings.tool_timeout_seconds,
            )
        except ProviderError as exc:
            metrics.invoke_rejected_total.labels("provider_error").inc()
            metrics.invoke_total.labels(provider.name, "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Search provider error: {exc}",
                status_code=502,
            ) from exc
        except TimeoutError as exc:
            metrics.invoke_rejected_total.labels("provider_error").inc()
            metrics.invoke_total.labels(provider.name, "error").inc()
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Search provider timed out after {settings.tool_timeout_seconds}s.",
                status_code=504,
            ) from exc

    body: dict[str, Any] = {
        "tool": manifest_svc.TOOL_NAME,
        "result": {"results": [r.to_dict() for r in results]},
    }

    # ── (7) 10 MiB output cap ───────────────────────────────────────────────────
    serialized = json.dumps(body)
    size = len(serialized.encode("utf-8"))
    if size > settings.max_output_bytes:
        metrics.invoke_rejected_total.labels("output_too_large").inc()
        metrics.invoke_total.labels(provider.name, "error").inc()
        logger.info("invoke_output_too_large", bytes=size, max_bytes=settings.max_output_bytes)
        raise ApiError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"Search result ({size} bytes) exceeds the {settings.max_output_bytes}-byte output cap.",
            status_code=413,
            details={
                "reason": "OUTPUT_BYTES_EXCEEDED",
                "bytes": size,
                "max_bytes": settings.max_output_bytes,
            },
        )

    metrics.invoke_total.labels(provider.name, "ok").inc()

    # ── (8) Store for idempotent replay ─────────────────────────────────────────
    await idempotency.store(valkey, idem_key, principal, 200, body, settings=settings)

    return _json_response(body, 200, serialized=serialized)


async def _read_json(request: Request) -> Any:
    """Parse the request body as JSON; malformed -> 422 VALIDATION_ERROR."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "Request body is not valid JSON.", status_code=422
        ) from exc


def _json_response(
    body: dict[str, Any],
    status_code: int,
    *,
    serialized: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    content = serialized if serialized is not None else json.dumps(body)
    return Response(
        content=content,
        status_code=status_code,
        media_type="application/json",
        headers=extra_headers,
    )
