"""POST /mcp — spec-compliant MCP (Streamable HTTP, JSON-RPC 2.0) for mcp-eng-memory.

The MCP tool wire (the sole tool wire) for the 8 read-only, source-cited engineering-memory
tools. Speaks ``initialize`` / ``tools/list`` / ``tools/call`` / ``ping`` over JSON-RPC 2.0,
reusing the platform auth (coarse ``tool:invoke`` via ``require_principal`` + fine
``tool:mcp-eng-memory:invoke`` in ``tools/call``), the manifest input-schema validation, and
the cypherx-a1 backend dispatch. Stateless: no Valkey / idempotency / rate-limit (revocation
is enforced at the backend it forwards to). All tools are READ-ONLY and source-citing.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, Response

from ..core import metrics, trace
from ..core.auth import Principal, require_principal
from ..core.config import Settings, get_settings
from ..core.errors import ApiError, ErrorCode
from ..services import manifest as manifest_svc
from ..services import mcp_protocol
from ..services.backend import BackendClient

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp"])

# Retryable at the transport level: a 5xx tool fault OR 429 backpressure. The agent's MCP
# client reads ``_meta.retryable`` to drive its retry / circuit breaker.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


@router.post("/mcp")
async def mcp_endpoint(
    request: Request, principal: Principal = Depends(require_principal)
) -> Response:
    """MCP Streamable-HTTP entry: dispatch a JSON-RPC message (or batch) and respond."""
    settings = get_settings()
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > settings.max_request_body_bytes:
        raise ApiError(
            ErrorCode.PAYLOAD_TOO_LARGE, "Request body exceeds the size cap.",
            details={"reason": "BODY_BYTES_EXCEEDED"},
        )

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
            resp = await _dispatch_msg(msg, request, principal, settings)
            if resp is not None:
                out.append(resp)
        return Response(status_code=202) if not out else _json(out)

    resp = await _dispatch_msg(payload, request, principal, settings)
    return Response(status_code=202) if resp is None else _json(resp)


async def _dispatch_msg(
    msg: Any, request: Request, principal: Principal, settings: Settings
) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return mcp_protocol.error_message(
            None, mcp_protocol.INVALID_REQUEST, "JSON-RPC message must be an object."
        )
    method = msg.get("method")
    msg_id = msg.get("id")
    if method is None:  # a client->server response; we issue none, so ignore.
        return None
    if msg_id is None:  # a notification (e.g. notifications/initialized): no response.
        logger.info("mcp_notification", method=method)
        return None

    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS, "'params' must be an object."
        )

    if method == "initialize":
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.initialize_result(
                params.get("protocolVersion"),
                server_name=manifest_svc.load_manifest()["name"],
                server_version=settings.service_version,
                instructions="Read-only, source-cited engineering-memory queries.",
            ),
        )
    if method == "ping":
        return mcp_protocol.result_message(msg_id, {})
    if method == "tools/list":
        return mcp_protocol.result_message(
            msg_id, mcp_protocol.tools_list_result(manifest_svc.load_manifest().get("tools", []))
        )
    if method == "tools/call":
        return await _tools_call(msg_id, params, request, principal, settings)

    return mcp_protocol.error_message(
        msg_id, mcp_protocol.METHOD_NOT_FOUND, f"Unknown method '{method}'."
    )


async def _tools_call(
    msg_id: Any, params: dict[str, Any], request: Request, principal: Principal, settings: Settings
) -> dict[str, Any]:
    """Handle ``tools/call`` — governed dispatch to the cypherx-a1 backend."""
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name not in manifest_svc.tools_by_name():
        return mcp_protocol.error_message(msg_id, mcp_protocol.INVALID_PARAMS, f"Unknown tool {name!r}.")
    if not isinstance(arguments, dict):
        return mcp_protocol.error_message(
            msg_id, mcp_protocol.INVALID_PARAMS, "'arguments' must be an object."
        )

    # ── Fine scope (Contract-4 dual-scope) — in-band isError so the agent won't retry. ──
    if not principal.has_scope(settings.fine_scope):
        metrics.invoke_rejected_total.labels("scope_denied").inc()
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                f"Token missing required scope '{settings.fine_scope}'.",
                code="FORBIDDEN", retryable=False,
            ),
        )

    try:
        manifest_svc.validate_input(name, arguments)
    except manifest_svc.SchemaViolation as exc:
        metrics.invoke_rejected_total.labels("schema_invalid").inc()
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                f"Input schema validation failed: {exc.message}",
                code="VALIDATION_ERROR", retryable=False, pointer=exc.pointer,
            ),
        )

    backend: BackendClient = request.app.state.backend
    started = time.monotonic()
    try:
        with metrics.invoke_duration_seconds.labels(name).time():
            output, citations = await _dispatch(name, arguments, backend, agent_jwt=principal.agent_jwt)
    except ApiError as exc:
        metrics.invoke_total.labels(name, "error").inc()
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                exc.message, code=exc.code, retryable=exc.status_code in _RETRYABLE_STATUS
            ),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    result_obj: dict[str, Any] = {
        "output": output, "citations": citations,
        "duration_ms": duration_ms, "trace_id": trace.trace_id_var.get(),
    }
    serialized = json.dumps(result_obj)
    if len(serialized.encode("utf-8")) > settings.max_output_bytes:
        metrics.invoke_rejected_total.labels("output_too_large").inc()
        return mcp_protocol.result_message(
            msg_id,
            mcp_protocol.tool_error(
                "Result exceeds the output cap.", code="PAYLOAD_TOO_LARGE", retryable=False
            ),
        )

    metrics.invoke_total.labels(name, "ok").inc()
    return mcp_protocol.result_message(msg_id, mcp_protocol.tool_success(serialized, structured=result_obj))


async def _dispatch(
    tool: str, args: dict[str, Any], backend: BackendClient, *, agent_jwt: str
) -> tuple[Any, list[Any]]:
    """Map an MCP tool call to the cypherx-a1 read-only graph/copilot API (source-cited)."""
    if tool == "who_owns":
        data = await backend.graph("/v1/graph/who-owns", {"target": args["target"]}, agent_jwt=agent_jwt)
        return {"items": data.get("items", [])}, data.get("citations", [])
    if tool == "why_built":
        data = await backend.graph("/v1/graph/why-built", {"topic": args["feature"]}, agent_jwt=agent_jwt)
        return {"items": data.get("items", [])}, data.get("citations", [])
    if tool == "what_breaks_if_changed":
        data = await backend.graph(
            "/v1/graph/what-breaks",
            {"target": args["target"], "max_hops": int(args.get("max_hops", 3))},
            agent_jwt=agent_jwt,
        )
        return {"items": data.get("items", [])}, data.get("citations", [])
    if tool == "experts_on":
        data = await backend.graph("/v1/graph/experts", {"topic": args["topic"]}, agent_jwt=agent_jwt)
        return {"items": data.get("items", [])}, data.get("citations", [])
    if tool == "graph_neighbors":
        data = await backend.graph(
            "/v1/graph/neighbors",
            {"target": args["target"], "max_hops": int(args.get("max_hops", 2))},
            agent_jwt=agent_jwt,
        )
        return {"items": data.get("items", [])}, data.get("citations", [])
    if tool == "what_changed":
        body: dict[str, Any] = {"target": args["target"]}
        if args.get("since"):
            body["since"] = args["since"]
        if args.get("until"):
            body["until"] = args["until"]
        data = await backend.graph("/v1/graph/activity", body, agent_jwt=agent_jwt)
        return {"items": data.get("items", [])}, data.get("citations", [])
    if tool == "incident_root_cause":
        data = await backend.ask(
            f"What was the root cause and remediation of this incident: {args['incident']}? "
            "Cite the evidence.",
            agent_jwt=agent_jwt,
        )
        return {"answer": data.get("answer", "")}, data.get("citations", [])
    if tool == "how_does_x_work":
        data = await backend.ask(f"How does {args['topic']} work in this codebase?", agent_jwt=agent_jwt)
        return {"answer": data.get("answer", "")}, data.get("citations", [])
    raise ApiError(ErrorCode.NOT_FOUND, f"Unhandled tool {tool!r}.")


def _json(body: dict[str, Any] | list[dict[str, Any]]) -> Response:
    return Response(content=json.dumps(body), media_type="application/json")
