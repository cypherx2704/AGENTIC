"""MCP (Streamable HTTP, JSON-RPC 2.0) tool wires for flow-tools.

Two routes, ONE governed invoke pipeline:

* ``POST /m/{mcp_slug}/mcp`` — the AGGREGATING MCP server: an MCP is a named collection of
  atomic tools registered as one server. ``tools/list`` surfaces every member; ``tools/call
  {name}`` routes to the member tool whose ``snake_name == name`` and runs it through the full
  governed pipeline. A tool may belong to several MCPs (many-to-many).
* ``POST /w/{slug}/mcp`` — the legacy single-tool wire for a flow-tool's slug (one binding =
  one tool). Kept working byte-for-byte for already-registered tools.

Both share the governed invoke pipeline (nothing is bypassed):

* **Auth + coarse scope** — ``require_principal`` (dual-mode JWT + WP03 revocation) gates the
  endpoint; the coarse ``tool:invoke`` scope is enforced on entry (Contract-2 403 otherwise).
* **Per-tool access grant** — the calling agent's registry access grant is resolved inside
  ``tools/call`` (``_resolve_tool_access``, keyed by (agent, server_name, capability=name)):
  deny only on an explicit ``none``, fail-open otherwise (xAgent enforces access fail-closed
  before it ever invokes).
* **RLS resolution / idempotency (+ in-flight lock) / rate-limit / input-schema / Node-RED
  dispatch / output-cap** — identical for both wires (the idempotency cache is shared, scoped
  per tenant + a wire-specific scope), so a flow fires at most once per Idempotency-Key.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, Response

from ..core import metrics
from ..core.auth import COARSE_INVOKE_SCOPE, Principal, require_any_scope, require_principal
from ..core.config import Settings, get_settings
from ..core.errors import ApiError
from ..db import pool as db_pool
from ..db import queries
from ..services import idempotency, manifest_builder, mcp_protocol, rate_limit, schema_validate
from ..services.nodered_adapter import NoderedError, invoke_workflow
from ..services.secrets import resolve_secret

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp"])

# Retryable at the transport level: a 5xx tool fault OR 429 backpressure. The agent's MCP
# client reads ``_meta.retryable`` to drive its retry / circuit breaker.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


# ── Entry points ─────────────────────────────────────────────────────────────────────


@router.post("/m/{mcp_slug}/mcp")
async def mcp_aggregating_endpoint(
    mcp_slug: str, request: Request, principal: Principal = Depends(require_principal)
) -> Response:
    """Aggregating MCP entry: dispatch a JSON-RPC message (or batch) against the MCP collection."""
    settings = get_settings()

    async def dispatch(msg: Any) -> dict[str, Any] | None:
        return await _dispatch_mcp(msg, request, principal, mcp_slug, settings)

    return await _handle_json_rpc(request, principal, dispatch)


@router.post("/w/{slug}/mcp")
async def mcp_endpoint(
    slug: str, request: Request, principal: Principal = Depends(require_principal)
) -> Response:
    """Legacy single-tool MCP entry for a flow-tool. Back-compat alias: a flow-tool's slug is ALSO
    its singleton MCP's slug (server_name ``tool-<slug>``), so this resolves the SAME MCP from the
    new source-of-truth model and dispatches through the SAME handler as ``/m`` — including the
    unified idempotency scope (finding #2), so a cross-wire retry with the same Idempotency-Key
    shares the record/lock and cannot double-fire."""
    settings = get_settings()

    async def dispatch(msg: Any) -> dict[str, Any] | None:
        return await _dispatch_mcp(msg, request, principal, slug, settings)

    return await _handle_json_rpc(request, principal, dispatch)


async def _handle_json_rpc(
    request: Request,
    principal: Principal,
    dispatch: Callable[[Any], Awaitable[dict[str, Any] | None]],
) -> Response:
    """Shared transport shell: coarse-scope gate + JSON parse + single/batch dispatch."""
    # Coarse scope (every tool caller holds ``tool:invoke``) — HTTP-level, before JSON-RPC.
    require_any_scope(principal, [COARSE_INVOKE_SCOPE])

    raw = await request.body()
    try:
        payload: Any = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return _json(mcp_protocol.error_message(None, mcp_protocol.PARSE_ERROR, "Invalid JSON body."))

    if payload is None:
        return _json(
            mcp_protocol.error_message(None, mcp_protocol.INVALID_REQUEST, "Empty request body.")
        )

    if isinstance(payload, list):
        out: list[dict[str, Any]] = []
        for msg in payload:
            resp = await dispatch(msg)
            if resp is not None:
                out.append(resp)
        return Response(status_code=202) if not out else _json(out)

    resp = await dispatch(payload)
    return Response(status_code=202) if resp is None else _json(resp)


def _parse_envelope(msg: Any) -> tuple[Any, Any, dict[str, Any] | None, dict[str, Any] | None]:
    """Validate a JSON-RPC envelope. Returns (method, msg_id, params, early_response). When
    ``method``/``params`` are set the caller routes; when ``early_response`` is set the caller
    returns it directly (an error, or ``None`` for a notification / server-response)."""
    if not isinstance(msg, dict):
        return None, None, None, mcp_protocol.error_message(
            None, mcp_protocol.INVALID_REQUEST, "JSON-RPC message must be an object."
        )
    method = msg.get("method")
    msg_id = msg.get("id")
    if method is None:  # a response to a server request — we issue none; ignore.
        return None, msg_id, None, None
    if msg_id is None:  # a notification (e.g. notifications/initialized): no response.
        return method, None, None, None
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return method, msg_id, None, mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS, "'params' must be an object."
        )
    return method, msg_id, params, None


# ── Aggregating MCP wire (POST /m/{mcp_slug}/mcp, and the /w/{slug}/mcp alias) ───────


def _idem_scope(server_name: str, capability: str) -> str:
    """The idempotency record-key + in-flight-lock scope: a PURE function of (server_name,
    capability), IDENTICAL across the ``/m`` and ``/w`` wires (finding #2). Because a singleton's
    server_name (``tool-<slug>``) + member ``snake_name`` are the same whether reached via
    ``/m/<slug>`` or ``/w/<slug>``, a cross-wire retry with the same Idempotency-Key hits the same
    record/lock and the side-effecting flow fires at most once."""
    return f"{server_name}:{capability}"


# ── Aggregating MCP wire (POST /m/{mcp_slug}/mcp) ────────────────────────────────────


async def _dispatch_mcp(
    msg: Any, request: Request, principal: Principal, mcp_slug: str, settings: Settings
) -> dict[str, Any] | None:
    """Route one JSON-RPC message against an MCP collection. Returns the response, or None."""
    method, msg_id, params, early = _parse_envelope(msg)
    if params is None:
        if method is not None and msg_id is None:
            logger.info("mcp_notification", method=method, mcp_slug=mcp_slug)
        return early

    if method == "initialize":
        loaded = await _load_mcp(request, principal, mcp_slug)
        server_name = loaded[0]["server_name"] if loaded is not None else mcp_slug
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.initialize_result(
                params.get("protocolVersion"),
                server_name=server_name,
                server_version=settings.service_version,
            ),
        )
    if method == "ping":
        return mcp_protocol.result_message(msg_id, {})
    if method == "tools/list":
        return await _tools_list_mcp(msg_id, request, principal, mcp_slug)
    if method == "tools/call":
        return await _tools_call_mcp(msg_id, params, request, principal, mcp_slug, settings)

    return mcp_protocol.error_message(
        msg_id, mcp_protocol.METHOD_NOT_FOUND, f"Unknown method '{method}'."
    )


async def _load_mcp(
    request: Request, principal: Principal, mcp_slug: str
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Resolve the tenant-scoped (RLS) ACTIVE MCP + its active member tools for ``mcp_slug``."""
    pool = request.app.state.db_pool

    async def _load(conn: Any) -> Any:
        return await queries.get_mcp_with_members(conn, mcp_slug)

    loaded = await db_pool.in_tenant(pool, principal.tenant_id, _load)
    if loaded is None or loaded[0]["status"] != "active":
        return None
    return loaded


def _find_member(members: list[dict[str, Any]], name: Any) -> dict[str, Any] | None:
    """Route by MCP tool name: the member whose ``snake_name == name`` (Contract-4)."""
    for tool in members:
        if tool["snake_name"] == name:
            return tool
    return None


async def _tools_list_mcp(
    msg_id: Any, request: Request, principal: Principal, mcp_slug: str
) -> dict[str, Any]:
    """List ALL member tools of the MCP (from the regenerated aggregating manifest)."""
    loaded = await _load_mcp(request, principal, mcp_slug)
    if loaded is None:
        return mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS, f"No active MCP for slug '{mcp_slug}'."
        )
    mcp_row, members = loaded
    manifest = manifest_builder.mcp_manifest_from_row(get_settings(), mcp_row, members)
    return mcp_protocol.result_message(msg_id, mcp_protocol.tools_list_result(manifest["tools"]))


async def _tools_call_mcp(
    msg_id: Any, params: dict[str, Any], request: Request, principal: Principal,
    mcp_slug: str, settings: Settings,
) -> dict[str, Any]:
    """Handle ``tools/call`` for the aggregating wire — route by ``name`` to a member, then invoke."""
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS, "'arguments' must be an object."
        )

    loaded = await _load_mcp(request, principal, mcp_slug)
    if loaded is None:
        metrics.invoke_rejected_total.labels("not_found", mcp_slug).inc()
        return mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS, f"No active MCP for slug '{mcp_slug}'."
        )
    mcp_row, members = loaded
    tool = _find_member(members, name)
    if tool is None:
        metrics.invoke_rejected_total.labels("not_found", mcp_row["slug"]).inc()
        return mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS,
            f"Unknown tool {name!r}; this MCP exposes {[t['snake_name'] for t in members]}.",
        )

    server_name = mcp_row["server_name"]
    capability = tool["snake_name"]
    # Per-member metric attribution (finding #1): mcp_slug:capability distinguishes members.
    metrics_slug = f"{mcp_row['slug']}:{capability}"
    return await _governed_invoke(
        msg_id, arguments, request, principal, settings,
        server_name=server_name, capability=capability,
        scope=_idem_scope(server_name, capability), metrics_slug=metrics_slug,
        log_ref=mcp_slug,
        input_schema=tool["input_schema"],
        http_method=tool["http_method"],
        http_path=tool["http_path"],
        internal_host=tool["internal_host"],
        http_node_root=tool["http_node_root"],
        invoke_secret_ref=tool["invoke_secret_ref"],
    )


# ── Shared governed invoke pipeline ──────────────────────────────────────────────────


async def _governed_invoke(
    msg_id: Any,
    arguments: dict[str, Any],
    request: Request,
    principal: Principal,
    settings: Settings,
    *,
    server_name: str,
    capability: str,
    scope: str,
    metrics_slug: str,
    log_ref: str,
    input_schema: dict[str, Any],
    http_method: str,
    http_path: str,
    internal_host: str,
    http_node_root: str,
    invoke_secret_ref: str,
) -> dict[str, Any]:
    """The governed invocation: access grant -> idempotency replay/lock -> rate-limit -> input
    schema -> Node-RED dispatch -> output cap -> idempotency store. Shared by BOTH the legacy
    single-tool wire and the aggregating MCP wire so both authorize + dispatch identically."""
    # ── Per-tool authorization via the registry ACCESS GRANT — deny only explicit 'none'
    #    (fail-open otherwise; xAgent enforces access fail-closed before invoking). ──────
    access = await _resolve_tool_access(
        request, principal, server_name=server_name, capability=capability, settings=settings
    )
    if access == "none":
        metrics.invoke_rejected_total.labels("access_denied", metrics_slug).inc()
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                "This agent is not granted access to the tool. Grant it in the agent's tool access.",
                code="FORBIDDEN", retryable=False,
            ),
        )

    valkey = getattr(request.app.state, "valkey", None)
    idem_key = request.headers.get("idempotency-key")

    # ── Idempotency replay (shared cache; scoped tenant+wire) ───────────────────────────
    replay = await idempotency.get_replay(valkey, idem_key, principal, scope=scope, settings=settings)
    if replay is not None:
        logger.info("mcp_invoke_replayed", tenant_id=principal.tenant_id, ref=log_ref)
        return mcp_protocol.result_message(msg_id, _result_from_body(replay.body))

    # ── In-flight lock — a concurrent duplicate (retry before the first finishes) must not
    #    double-fire the side-effecting flow. Held => retryable, so the agent retries into the
    #    stored replay. ───────────────────────────────────────────────────────────────────
    if not await idempotency.acquire_inflight(valkey, idem_key, principal, scope=scope, settings=settings):
        metrics.invoke_rejected_total.labels("in_flight", metrics_slug).inc()
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                "A request with this Idempotency-Key is already in progress; retry shortly.",
                code="IDEMPOTENCY_REQUEST_IN_FLIGHT", retryable=True,
            ),
        )

    try:
        await rate_limit.enforce(valkey, principal, dimension="invoke", settings=settings)

        try:
            schema_validate.validate(arguments, input_schema)
        except schema_validate.SchemaViolation as exc:
            metrics.invoke_rejected_total.labels("schema_invalid", metrics_slug).inc()
            return mcp_protocol.result_message(
                msg_id,
                mcp_protocol.tool_error(
                    f"Input schema validation failed: {exc.message}",
                    code="VALIDATION_ERROR", retryable=False, pointer=exc.pointer,
                ),
            )

        secret = resolve_secret(invoke_secret_ref, settings)
        client = request.app.state.http_client
        with metrics.invoke_duration_seconds.time():
            try:
                result = await invoke_workflow(
                    client,
                    internal_host=internal_host,
                    http_node_root=http_node_root,
                    http_path=http_path,
                    method=http_method,
                    args=arguments,
                    secret=secret,
                    secret_header=settings.nodered_invoke_secret_header,
                    timeout=settings.nodered_invoke_timeout_seconds,
                    trace_headers=_trace_headers(request),
                )
            except NoderedError as exc:
                metrics.invoke_rejected_total.labels("nodered_error", metrics_slug).inc()
                metrics.invoke_total.labels(metrics_slug, "error").inc()
                code = "SERVICE_UNAVAILABLE" if exc.retryable else "VALIDATION_ERROR"
                return mcp_protocol.result_message(
                    msg_id, mcp_protocol.tool_error(exc.message, code=code, retryable=exc.retryable)
                )

        body: dict[str, Any] = {"tool": capability, "result": result}

        serialized = json.dumps(body)
        if len(serialized.encode("utf-8")) > settings.max_output_bytes:
            metrics.invoke_rejected_total.labels("output_too_large", metrics_slug).inc()
            metrics.invoke_total.labels(metrics_slug, "error").inc()
            return mcp_protocol.result_message(
                msg_id,
                mcp_protocol.tool_error(
                    "Workflow result exceeds the output cap.",
                    code="PAYLOAD_TOO_LARGE", retryable=False,
                ),
            )

        metrics.invoke_total.labels(metrics_slug, "ok").inc()
        await idempotency.store(
            valkey, idem_key, principal, 200, body, scope=scope, settings=settings
        )
        return mcp_protocol.result_message(msg_id, _result_from_body(body))

    except ApiError as exc:
        # Rate-limit (429) or another typed platform error: preserve retryability for the agent.
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                exc.message, code=exc.code, retryable=exc.status_code in _RETRYABLE_STATUS
            ),
        )
    finally:
        await idempotency.release_inflight(
            valkey, idem_key, principal, scope=scope, settings=settings
        )


def _result_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """Shape a stored/fresh ``{tool, result}`` body into an MCP ``tools/call`` result."""
    inner = body.get("result")
    structured = inner if isinstance(inner, dict) else {"result": inner}
    return mcp_protocol.tool_success(json.dumps(structured), structured=structured)


async def _resolve_tool_access(
    request: Request,
    principal: Principal,
    *,
    server_name: str,
    capability: str,
    settings: Settings,
) -> str | None:
    """The calling agent's registry access mode for this tool, or ``None`` when it can't be
    determined. ``None`` => FAIL-OPEN (allow): xAgent already enforces access fail-closed before
    it ever invokes, so a registry blip / missing forwarded JWT must never break a live tool. Only
    an explicit ``'none'`` denies. (Module-level so tests can monkeypatch it.)"""
    if not settings.enforce_registry_access:
        return None
    registry = getattr(request.app.state, "registry", None)
    agent_jwt = request.headers.get("x-forwarded-agent-jwt") or _bearer_token(request)
    if registry is None or not principal.agent_id or not agent_jwt:
        return None
    try:
        return await registry.get_tool_access(
            user_jwt=agent_jwt,
            agent_id=principal.agent_id,
            name=server_name,
            capability=capability,
            trace_headers=_trace_headers(request),
        )
    except ApiError as exc:
        logger.warning("tool_access_check_failed_open", server=server_name, error=exc.message)
        return None


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    parts = auth.split(" ", 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""


def _trace_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in ("traceparent", "x-request-id", "tracestate"):
        v = request.headers.get(h)
        if v:
            out[h] = v
    return out


def _json(body: dict[str, Any] | list[dict[str, Any]]) -> Response:
    return Response(content=json.dumps(body), media_type="application/json")
