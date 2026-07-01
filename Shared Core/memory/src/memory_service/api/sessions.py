"""Session endpoints — POST /v1/sessions.

Sessions are keyed by ``session_id`` and OWNED by a principal. Create is IDEMPOTENT for
the same (principal, session_id): a repeat returns the existing session (200/201 alike).
A DIFFERENT principal claiming an already-used ``session_id`` is a cross-principal
collision -> 409 CONFLICT (the id is taken by someone else; an end user must never be
able to attach to another principal's session).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core.auth import SCOPE_WRITE, Principal, require_scope
from ..core.errors import ApiError, ErrorCode
from ..models.memory import CreateSessionRequest, SessionRecord
from ..services import repository

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["sessions"])


def _repo(request: Request) -> repository.MemoryRepository:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Memory backend is unavailable.")
    return repo


@router.post("/sessions", status_code=201, response_model=None)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_WRITE)),
) -> JSONResponse:
    repo = _repo(request)
    ptype, pid = principal.memory_principal
    session = repository.Session(
        session_id=body.session_id,
        tenant_id=principal.tenant_id,
        principal_type=ptype,
        principal_id=pid,
        title=body.title,
        metadata=body.metadata,
        created_at=datetime.now(UTC),
    )
    stored, ok = await repo.create_session(session=session)
    if not ok:
        # The session_id exists under a DIFFERENT principal — cross-principal collision.
        raise ApiError(
            ErrorCode.CONFLICT,
            "session_id already exists for a different principal.",
            details={"reason": "SESSION_PRINCIPAL_COLLISION", "session_id": body.session_id},
        )
    record = SessionRecord(
        session_id=stored.session_id,
        principal_type=stored.principal_type,
        principal_id=stored.principal_id,
        title=stored.title,
        metadata=stored.metadata,
        created_at=stored.created_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )
    return JSONResponse(content=record.model_dump(), status_code=201)
