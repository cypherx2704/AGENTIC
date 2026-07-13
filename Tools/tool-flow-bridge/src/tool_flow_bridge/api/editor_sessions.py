"""Editor-session API — provisions the tenant's Node-RED and tells the caller how to embed it.

* ``POST /v1/editor-sessions`` — frontend-facing (via the BFF). Ensures the tenant's Node-RED
  instance exists and returns readiness + the iframe path the SPA should load
  (``/bff/nodered/`` — the BFF proxy). Requires ``tool:admin``.
* ``GET  /v1/editor-runtime``  — BFF-facing. Returns the routing target the BFF's
  ``/bff/nodered/*`` proxy needs: the Node-RED ``internal_host``, admin root, HTTP-In root,
  and the admin bearer token to inject (never exposed to the browser). Requires ``tool:admin``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core.auth import ADMIN_SCOPES, Principal, require_any_scope, require_principal
from ..core.config import get_settings
from ..services.provisioner import ensure_runtime
from ..services.secrets import resolve_secret

router = APIRouter(prefix="/v1", tags=["editor"])


def _expiry_iso(ttl_seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")


@router.post("/editor-sessions")
async def open_editor_session(
    request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    require_any_scope(principal, ADMIN_SCOPES)
    settings = get_settings()
    runtime = await ensure_runtime(
        request.app.state.db_pool,
        principal.tenant_id,
        request.app.state.provisioner,
        settings,
    )
    return JSONResponse(
        {
            "ready": runtime["status"] == "running",
            "runtime_status": runtime["status"],
            "editor_url": "/bff/nodered/",
            "expires_at": _expiry_iso(settings.editor_session_ttl_seconds),
        }
    )


@router.get("/flows")
async def list_flows(
    request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    """List the tenant's Node-RED flow tabs (the Publish dialog's workflow picker)."""
    require_any_scope(principal, ADMIN_SCOPES)
    flows = await request.app.state.publisher.list_flows(principal)
    return {"data": flows}


@router.get("/editor-runtime")
async def get_editor_runtime(
    request: Request, principal: Principal = Depends(require_principal)
) -> dict:
    """BFF-only: resolve the Node-RED routing target + admin token for the session tenant."""
    require_any_scope(principal, ADMIN_SCOPES)
    settings = get_settings()
    runtime = await ensure_runtime(
        request.app.state.db_pool,
        principal.tenant_id,
        request.app.state.provisioner,
        settings,
    )
    return {
        "internal_host": runtime["internal_host"],
        "admin_root": settings.nodered_admin_root,
        "http_node_root": runtime["http_node_root"],
        "admin_token": resolve_secret(runtime["admin_token_ref"], settings),
        "runtime_status": runtime["status"],
    }
