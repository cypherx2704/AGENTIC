"""Publish + manage flow-tools — the control-plane API called by the user (via the BFF).

* ``POST   /v1/flow-tools``        — publish (or re-publish) a Node-RED flow as an MCP tool.
* ``GET    /v1/flow-tools``        — list this tenant's published tools.
* ``GET    /v1/flow-tools/{slug}`` — one tool's detail.
* ``DELETE /v1/flow-tools/{slug}`` — unpublish (retire) a tool.

Scopes: publishing/registering needs ``tool:admin`` (or ``platform:admin``); a non-
``automated`` access default also needs ``tenant:admin`` (or ``platform:admin``) because it
marks the registry tool restricted. Listing/detail need only an authenticated tenant
principal.
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

router = APIRouter(prefix="/v1/flow-tools", tags=["flow-tools"])


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


@router.post("")
async def publish(request: Request, principal: Principal = Depends(require_principal)) -> JSONResponse:
    settings = get_settings()
    require_any_scope(principal, ADMIN_SCOPES)

    body = await _read_json(request)
    tool = body.get("tool") or {}
    access_mode = str((tool or {}).get("access_mode") or settings.default_access_mode).lower()
    # A non-automated default marks the registry tool restricted -> needs tenant:admin.
    if access_mode != "automated":
        require_any_scope(principal, TENANT_ADMIN_SCOPES)

    valkey = getattr(request.app.state, "valkey", None)
    await rate_limit.enforce(
        valkey, principal, dimension="publish", limit=settings.publish_rate_limit_per_min
    )

    publisher = request.app.state.publisher
    result = await publisher.publish(
        principal, _raw_bearer(request), body, trace_headers=_trace_headers(request)
    )
    return JSONResponse(result, status_code=201)


@router.get("")
async def list_tools(request: Request, principal: Principal = Depends(require_principal)) -> dict:
    publisher = request.app.state.publisher
    return {"data": await publisher.list_tools(principal)}


@router.get("/{slug}")
async def get_tool(
    slug: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    publisher = request.app.state.publisher
    return await publisher.get_tool(principal, slug)


@router.delete("/{slug}")
async def unpublish(
    slug: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    require_any_scope(principal, ADMIN_SCOPES)
    publisher = request.app.state.publisher
    return await publisher.unpublish(
        principal, _raw_bearer(request), slug, trace_headers=_trace_headers(request)
    )


@router.post("/{slug}/test")
async def test_tool(
    slug: str, request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    """Run a published tool with sample args (owner-only) — the UI's 'Test' action."""
    require_any_scope(principal, ADMIN_SCOPES)
    body = await _read_json(request)
    raw = body.get("args")
    args = raw if isinstance(raw, dict) else {k: v for k, v in body.items() if k != "args"}
    return await request.app.state.publisher.test_tool(principal, slug, args)
