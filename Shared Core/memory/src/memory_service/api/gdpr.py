"""GDPR bulk wipe — POST /v1/gdpr/wipe.

Right-to-erasure for a principal's memories. In ONE transaction (atomic by construction
in the repository) the wipe:

  1. writes a ``gdpr_wipe_log`` audit row,
  2. DELETEs every memory (and session) owned by the target principal,
  3. emits a ``cypherx.memory.gdpr.wiped`` outbox event.

Either auth mode is accepted (agent or service). By default the CALLER wipes their OWN
principal; an explicit ``principal_type``/``principal_id`` lets a privileged caller wipe
another principal in the same tenant (RLS still scopes the DELETE to the tenant).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core import metrics, trace
from ..core.auth import SCOPE_WRITE, Principal, require_scope
from ..core.errors import ApiError, ErrorCode
from ..models.memory import GdprWipeRequest, GdprWipeResponse
from ..services import repository

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["gdpr"])


def _repo(request: Request) -> repository.MemoryRepository:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Memory backend is unavailable.")
    return repo


@router.post("/gdpr/wipe", response_model=None)
async def gdpr_wipe(
    body: GdprWipeRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_WRITE)),
) -> JSONResponse:
    settings = request.app.state.settings
    repo = _repo(request)

    caller_type, caller_id = principal.memory_principal
    # Default to the caller's own principal; explicit target requires BOTH fields.
    if body.principal_type or body.principal_id:
        if not (body.principal_type and body.principal_id):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "principal_type and principal_id must be provided together.",
            )
        target_type, target_id = body.principal_type, body.principal_id
    else:
        target_type, target_id = caller_type, caller_id

    requested_by = f"{caller_type}:{caller_id}"
    result = await repo.gdpr_wipe(
        tenant_id=principal.tenant_id,
        principal_type=target_type,
        principal_id=target_id,
        requested_by=requested_by,
        reason=body.reason,
        trace_id=trace.trace_id_var.get(),
        producer_version=settings.service_version,
    )
    metrics.gdpr_wiped_total.inc()
    resp = GdprWipeResponse(
        principal_type=target_type,
        principal_id=target_id,
        deleted_count=result.deleted_count,
        wipe_log_id=result.wipe_log_id,
    )
    return JSONResponse(content=resp.model_dump())
