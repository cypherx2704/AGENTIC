"""Atomic-tool + MCP-collection management API (Phase 2 control plane, called via the BFF).

Tools:
* ``POST /v1/tools``            — create an atomic tool from a Node-RED flow (auto-singleton MCP
  unless ``mcp_ids`` are given).
* ``GET  /v1/tools``            — this tenant's atomic tools + their MCP memberships.

MCP collections (aggregating servers):
* ``POST   /v1/mcps``           — create an MCP (ownership-validated ``tool_ids``) + register it.
* ``GET    /v1/mcps``           — this tenant's MCPs + members.
* ``PUT    /v1/mcps/{id}``      — update metadata/membership -> regenerate + re-register (version STABLE).
* ``POST   /v1/mcps/{id}/publish`` — (re)register/refresh in the registry.
* ``DELETE /v1/mcps/{id}``      — unpublish: retire the MCP AND its exclusively-owned tools.
* ``POST   /v1/mcps/{id}/promote`` — the SOLE path to Public (platform namespace, ``platform:admin``).

Scopes: writes need ``tool:admin`` (or ``platform:admin``); a non-``automated`` tool default also
needs ``tenant:admin`` (it marks the singleton restricted); promote needs ``platform:admin``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core.auth import (
    ADMIN_SCOPES,
    TENANT_ADMIN_SCOPES,
    Principal,
    require_any_scope,
    require_principal,
)
from ..core.config import get_settings
from ..core.errors import ApiError, ErrorCode
from ..services import rate_limit

PLATFORM_ADMIN_SCOPES = ("platform:admin",)

tools_router = APIRouter(prefix="/v1/tools", tags=["tools"])
mcps_router = APIRouter(prefix="/v1/mcps", tags=["mcps"])


def _raw_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    _, _, token = auth.partition(" ")
    return token.strip()


def _trace_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in ("traceparent", "x-request-id", "tracestate"):
        v = request.headers.get(h)
        if v:
            out[h] = v
    return out


async def _read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "Request body is not valid JSON.", status_code=422
        ) from exc
    if not isinstance(data, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Request body must be an object.", status_code=422)
    return data


# ── Tools ─────────────────────────────────────────────────────────────────────────
@tools_router.post("")
async def create_tool(
    request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    settings = get_settings()
    require_any_scope(principal, ADMIN_SCOPES)
    body = await _read_json(request)
    access_mode = str(body.get("access_mode") or settings.default_access_mode).lower()
    # A non-automated default marks the (singleton) registry tool restricted -> needs tenant:admin.
    if access_mode != "automated" and not body.get("mcp_ids"):
        require_any_scope(principal, TENANT_ADMIN_SCOPES)
    valkey = getattr(request.app.state, "valkey", None)
    await rate_limit.enforce(
        valkey, principal, dimension="publish", limit=settings.publish_rate_limit_per_min
    )
    publisher = request.app.state.publisher
    result = await publisher.create_tool(
        principal, _raw_bearer(request), body, trace_headers=_trace_headers(request)
    )
    return JSONResponse(result, status_code=201)


@tools_router.get("")
async def list_tools(request: Request, principal: Principal = Depends(require_principal)) -> dict:
    publisher = request.app.state.publisher
    return {"data": await publisher.list_tools(principal)}


# ── MCP collections ─────────────────────────────────────────────────────────────────
@mcps_router.post("")
async def create_mcp(
    request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    settings = get_settings()
    require_any_scope(principal, ADMIN_SCOPES)
    valkey = getattr(request.app.state, "valkey", None)
    await rate_limit.enforce(
        valkey, principal, dimension="publish", limit=settings.publish_rate_limit_per_min
    )
    body = await _read_json(request)
    publisher = request.app.state.publisher
    result = await publisher.create_mcp(
        principal, _raw_bearer(request), body, trace_headers=_trace_headers(request)
    )
    return JSONResponse(result, status_code=201)


@mcps_router.get("")
async def list_mcps(request: Request, principal: Principal = Depends(require_principal)) -> dict:
    publisher = request.app.state.publisher
    return {"data": await publisher.list_mcps(principal)}


@mcps_router.put("/{mcp_id}")
async def update_mcp(
    mcp_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    require_any_scope(principal, ADMIN_SCOPES)
    body = await _read_json(request)
    publisher = request.app.state.publisher
    return await publisher.update_mcp(
        principal, _raw_bearer(request), mcp_id, body, trace_headers=_trace_headers(request)
    )


@mcps_router.post("/{mcp_id}/publish")
async def publish_mcp(
    mcp_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    require_any_scope(principal, ADMIN_SCOPES)
    publisher = request.app.state.publisher
    return await publisher.publish_mcp(
        principal, _raw_bearer(request), mcp_id, trace_headers=_trace_headers(request)
    )


@mcps_router.delete("/{mcp_id}")
async def unpublish_mcp(
    mcp_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    require_any_scope(principal, ADMIN_SCOPES)
    publisher = request.app.state.publisher
    return await publisher.unpublish_mcp(
        principal, _raw_bearer(request), mcp_id, trace_headers=_trace_headers(request)
    )


@mcps_router.post("/{mcp_id}/promote")
async def promote_mcp(
    mcp_id: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    # Public is a platform action: promote registers under the platform (tenant_id NULL) namespace.
    require_any_scope(principal, PLATFORM_ADMIN_SCOPES)
    publisher = request.app.state.publisher
    return await publisher.promote_mcp(
        principal, _raw_bearer(request), mcp_id, trace_headers=_trace_headers(request)
    )
