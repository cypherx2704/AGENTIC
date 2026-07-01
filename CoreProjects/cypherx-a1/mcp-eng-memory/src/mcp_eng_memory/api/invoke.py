"""POST /mcp/v1/invoke — Contract-4 tool invocation.

Pipeline: auth (coarse ``tool:invoke``) → dual fine-scope ``tool:mcp-eng-memory:invoke`` →
body-size cap → parse ``{tool, args}`` → input-schema validation (422 + JSON Pointer) →
dispatch to the cypherx-a1 backend (graph query or copilot ask) → output cap → cited result.

The result envelope is ``{tool, output, citations, duration_ms, trace_id}``. All tools are
READ-ONLY and source-citing. This server is stateless: NO metering is emitted here — the
calling agent's (xAgent) outbox owns per-invocation metering (Contract 14).
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request, Response

from ..core import metrics, trace
from ..core.auth import Principal, require_principal
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..services import manifest as manifest_svc
from ..services.backend import BackendClient

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["mcp"])


@router.post("/mcp/v1/invoke")
async def invoke(request: Request, principal: Principal = Depends(require_principal)) -> Response:
    settings = get_settings()

    # Fine-grained scope (Contract-4).
    if not principal.has_scope(settings.fine_scope):
        metrics.invoke_rejected_total.labels("scope_denied").inc()
        raise ApiError(ErrorCode.FORBIDDEN, f"Token missing required scope '{settings.fine_scope}'.")

    # Body-size cap (Content-Length).
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > settings.max_request_body_bytes:
        raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "Request body exceeds the size cap.",
                       details={"reason": "BODY_BYTES_EXCEEDED"})

    payload = await _read_json(request)
    if not isinstance(payload, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Request body must be a JSON object.")
    tool = payload.get("tool")
    if not tool or tool not in manifest_svc.tools_by_name():
        raise ApiError(ErrorCode.NOT_FOUND, f"Unknown tool {tool!r}.")
    args = payload.get("args")
    if args is None:
        args = payload.get("arguments")
    if args is None:
        args = {k: v for k, v in payload.items() if k not in {"tool", "args", "arguments"}}
    if not isinstance(args, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Invoke 'args' must be a JSON object.")

    try:
        manifest_svc.validate_input(tool, args)
    except manifest_svc.SchemaViolation as exc:
        metrics.invoke_rejected_total.labels("schema_invalid").inc()
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Input schema validation failed: {exc.message}",
                       details={"pointer": exc.pointer, "reason": exc.message}) from exc

    backend: BackendClient = request.app.state.backend
    started = time.monotonic()
    with metrics.invoke_duration_seconds.labels(tool).time():
        output, citations = await _dispatch(tool, args, backend, agent_jwt=principal.agent_jwt)
    duration_ms = int((time.monotonic() - started) * 1000)

    body: dict[str, Any] = {
        "tool": tool,
        "output": output,
        "citations": citations,
        "duration_ms": duration_ms,
        "trace_id": trace.trace_id_var.get(),
    }
    serialized = json.dumps(body)
    if len(serialized.encode("utf-8")) > settings.max_output_bytes:
        metrics.invoke_rejected_total.labels("output_too_large").inc()
        raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "Result exceeds the output cap.",
                       details={"reason": "OUTPUT_BYTES_EXCEEDED"})

    metrics.invoke_total.labels(tool, "ok").inc()
    return Response(content=serialized, status_code=200, media_type="application/json")


async def _dispatch(
    tool: str, args: dict[str, Any], backend: BackendClient, *, agent_jwt: str
) -> tuple[Any, list[Any]]:
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


async def _read_json(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Request body is not valid JSON.") from exc
