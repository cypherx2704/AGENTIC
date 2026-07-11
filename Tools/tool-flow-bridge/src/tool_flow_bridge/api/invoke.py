"""POST /w/{slug}/mcp/v1/invoke — Contract-4 tool invocation for a published workflow.

Pipeline (in order):

1.  **Auth** — ``require_principal`` verifies the JWT (dual-mode) + WP03 revocation mirror.
2.  **Coarse scope** — the caller must hold ``tool:invoke`` (403 otherwise).
3.  **Resolve binding** — look up the (tenant-scoped, RLS-protected) binding + runtime by
    slug; 404 if the caller's tenant has no active tool for that slug.
4.  **Fine scope** — the caller must ALSO hold ``tool:tool-<slug>:invoke`` (403 otherwise).
5.  **Idempotency replay** — a repeated ``Idempotency-Key`` (per tenant + slug) replays the
    stored result with header ``Idempotency-Replayed: true`` (essential — flows are
    side-effecting and xAgent retries 5xx with the same key).
6.  **Rate limit** — per-tenant Valkey fixed window (fail-open).
7.  **input_schema validation** — args validated against the stored ``input_schema`` (422 +
    JSON Pointer on failure).
8.  **Dispatch** — the Node-RED adapter POSTs the args to the workflow's HTTP-In endpoint,
    bounded by the tool timeout; the JSON response becomes the tool ``result``.
9.  **10 MiB output cap** — over -> 413 PAYLOAD_TOO_LARGE.
10. **Store** — cache the result under the Idempotency-Key for replay.

Request body (Contract-4 invoke envelope): ``{ "tool": "<snake>", "args": { ... } }``.
``tool`` is optional (single tool per slug); if present it must equal the tool's snake_name.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, Response

from ..core import metrics
from ..core.auth import COARSE_INVOKE_SCOPE, Principal, require_any_scope, require_principal
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..db import pool as db_pool
from ..db import queries
from ..services import idempotency, rate_limit, schema_validate
from ..services.nodered_adapter import NoderedError, invoke_workflow
from ..services.secrets import resolve_secret

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp"])


def _extract_args(payload: Any, snake_name: str) -> dict[str, Any]:
    """Pull invoke args out of the Contract-4 envelope (``args`` or ``arguments``)."""
    if not isinstance(payload, dict):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "Request body must be a JSON object.", status_code=422
        )
    tool = payload.get("tool")
    if tool is not None and tool != snake_name:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            f"Unknown tool {tool!r}; this endpoint exposes '{snake_name}'.",
        )
    args = payload.get("args")
    if args is None:
        args = payload.get("arguments")
    if args is None:
        args = {k: v for k, v in payload.items() if k not in {"tool", "args", "arguments"}}
    if not isinstance(args, dict):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "Invoke 'args' must be a JSON object.", status_code=422
        )
    return args


@router.post("/w/{slug}/mcp/v1/invoke")
async def invoke(
    slug: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Response:
    settings = get_settings()

    # ── (2) Coarse scope ────────────────────────────────────────────────────────
    require_any_scope(principal, [COARSE_INVOKE_SCOPE])

    # ── (3) Resolve the binding + runtime (RLS-scoped to the caller's tenant) ────
    pool = request.app.state.db_pool

    async def _load(conn):
        return await queries.get_binding_with_runtime(conn, slug)

    binding = await db_pool.in_tenant(pool, principal.tenant_id, _load)
    if binding is None or binding["status"] != "active":
        metrics.invoke_rejected_total.labels("not_found").inc()
        raise ApiError(ErrorCode.NOT_FOUND, f"No tool for slug '{slug}'.")

    # ── (4) Fine scope tool:tool-<slug>:invoke ──────────────────────────────────
    fine_scope = f"tool:tool-{slug}:invoke"
    if not principal.has_scope(fine_scope):
        metrics.invoke_rejected_total.labels("scope_denied").inc()
        raise ApiError(ErrorCode.FORBIDDEN, f"Token missing required scope '{fine_scope}'.")

    snake_name = binding["snake_name"]
    valkey = getattr(request.app.state, "valkey", None)
    idem_key = request.headers.get("idempotency-key")

    # ── (5) Idempotency replay ──────────────────────────────────────────────────
    replay = await idempotency.get_replay(valkey, idem_key, principal, scope=slug, settings=settings)
    if replay is not None:
        logger.info("invoke_replayed", tenant_id=principal.tenant_id, slug=slug)
        return _json_response(
            replay.body, replay.status_code, extra_headers={idempotency.REPLAY_HEADER: "true"}
        )

    # ── (5b) In-flight lock — stop a CONCURRENT duplicate (xAgent retrying a slow call before
    #        the first finishes) from BOTH dispatching to the side-effecting flow. If the lock is
    #        held, reject with a RETRYABLE 503 so xAgent retries and then hits the stored replay.
    if not await idempotency.acquire_inflight(valkey, idem_key, principal, scope=slug, settings=settings):
        metrics.invoke_rejected_total.labels("in_flight").inc()
        raise ApiError(
            ErrorCode.IDEMPOTENCY_REQUEST_IN_FLIGHT,
            "A request with this Idempotency-Key is already in progress; retry shortly.",
            status_code=503,
            headers={"Retry-After": "1"},
        )

    try:
        # ── (6) Rate limit (fail-open) ──────────────────────────────────────────
        await rate_limit.enforce(valkey, principal, dimension="invoke", settings=settings)

        # ── parse + (7) input_schema validation ─────────────────────────────────
        payload = await _read_json(request)
        args = _extract_args(payload, snake_name)
        try:
            schema_validate.validate(args, binding["input_schema"])
        except schema_validate.SchemaViolation as exc:
            metrics.invoke_rejected_total.labels("schema_invalid").inc()
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"Input schema validation failed: {exc.message}",
                status_code=422,
                details={"pointer": exc.pointer, "reason": exc.message},
            ) from exc

        # ── (8) Dispatch to the tenant's Node-RED workflow ──────────────────────
        secret = resolve_secret(binding["invoke_secret_ref"], settings)
        trace_headers = _trace_headers(request)
        client = request.app.state.http_client
        with metrics.invoke_duration_seconds.time():
            try:
                result = await invoke_workflow(
                    client,
                    internal_host=binding["internal_host"],
                    http_node_root=binding["http_node_root"],
                    http_path=binding["http_path"],
                    method=binding["http_method"],
                    args=args,
                    secret=secret,
                    secret_header=settings.nodered_invoke_secret_header,
                    timeout=settings.nodered_invoke_timeout_seconds,
                    trace_headers=trace_headers,
                )
            except NoderedError as exc:
                metrics.invoke_rejected_total.labels("nodered_error").inc()
                metrics.invoke_total.labels(slug, "error").inc()
                if exc.retryable:
                    # 5xx -> xAgent retries with the same Idempotency-Key (replay-safe).
                    raise ApiError(
                        ErrorCode.SERVICE_UNAVAILABLE, exc.message, status_code=502
                    ) from exc
                # 4xx -> terminal; xAgent will not retry.
                raise ApiError(
                    ErrorCode.VALIDATION_ERROR, exc.message, status_code=422
                ) from exc

        body: dict[str, Any] = {"tool": snake_name, "result": result}

        # ── (9) 10 MiB output cap ───────────────────────────────────────────────
        serialized = json.dumps(body)
        size = len(serialized.encode("utf-8"))
        if size > settings.max_output_bytes:
            metrics.invoke_rejected_total.labels("output_too_large").inc()
            metrics.invoke_total.labels(slug, "error").inc()
            raise ApiError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"Workflow result ({size} bytes) exceeds the {settings.max_output_bytes}-byte cap.",
                status_code=413,
                details={"reason": "OUTPUT_BYTES_EXCEEDED", "bytes": size,
                         "max_bytes": settings.max_output_bytes},
            )

        metrics.invoke_total.labels(slug, "ok").inc()

        # ── (10) Store for idempotent replay ────────────────────────────────────
        await idempotency.store(valkey, idem_key, principal, 200, body, scope=slug, settings=settings)

        return _json_response(body, 200, serialized=serialized)
    finally:
        # Release the in-flight lock so a legitimate later retry can proceed (the stored replay
        # covers a retry that arrives after a SUCCESS; the TTL is the backstop if this never runs).
        await idempotency.release_inflight(valkey, idem_key, principal, scope=slug, settings=settings)


def _trace_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in ("traceparent", "x-request-id", "tracestate"):
        v = request.headers.get(h)
        if v:
            out[h] = v
    return out


async def _read_json(request: Request) -> Any:
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
