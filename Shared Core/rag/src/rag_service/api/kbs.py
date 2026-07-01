"""Knowledge-base CRUD (Component 1) — POST/GET/list/DELETE /v1/kbs.

Creation-time alias resolution: the requested ``embedding_model_alias`` is resolved to a
literal model id + ``embedding_dim`` via the llms-gateway (env-pinned fallback) and persisted
IMMUTABLY — repointing the alias later in llms never changes an existing KB. A default
``(tenant,'*')`` ACL row is written in the same transaction unless ``private`` is set.

Quota: ``kbs_max`` is enforced before create (413 QUOTA_EXCEEDED). KB CRUD requires the
``rag:admin`` scope; status/list of documents requires ``rag:query``.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query, Request

from ..core.auth import (
    SCOPE_ADMIN,
    SCOPE_QUERY,
    Principal,
    require_scope,
)
from ..core.config import PLATFORM_TENANT_ID, get_settings
from ..core.errors import ApiError, ErrorCode, parse_uuid
from ..db import repository
from ..models.api import (
    CreateKbRequest,
    KbResponse,
    KbStatusResponse,
)
from ..services import quota

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/kbs", tags=["knowledge-bases"])


def _pool(request: Request) -> object:
    return getattr(request.app.state, "db_pool", None)


def _require_pool(request: Request) -> object:
    pool = _pool(request)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Database is not available.")
    return pool


def _agent_jwt(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-agent-jwt")
    if fwd:
        return fwd
    auth = request.headers.get("authorization", "")
    return auth.partition(" ")[2].strip() or None


@router.post("", response_model=None, status_code=201)
async def create_kb(
    body: CreateKbRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_ADMIN)),
) -> KbResponse:
    pool = _require_pool(request)
    settings = request.app.state.settings

    # Quota: knowledge-base count cap (413 over).
    await quota.enforce_kbs_max(pool, principal, settings=settings)  # type: ignore[arg-type]

    # Creation-time alias resolution -> immutable (model_resolved, dim).
    embedder = request.app.state.embedder
    model_resolved, dim = await embedder.resolve_model(
        body.embedding_model_alias,
        on_behalf_of=principal.agent_id or principal.on_behalf_of,
        agent_jwt=_agent_jwt(request),
    )

    kb = await repository.create_kb(
        pool,  # type: ignore[arg-type]
        principal.tenant_id,
        name=body.name,
        description=body.description,
        chunking_strategy=body.chunking_strategy,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
        embedding_model_alias=body.embedding_model_alias,
        embedding_model_resolved=model_resolved,
        embedding_dim=dim,
        created_by=principal.agent_id or principal.tenant_id,
        private=body.private,
    )
    logger.info("kb_created", kb_id=kb["kb_id"], tenant=principal.tenant_id)
    return KbResponse(**kb)


@router.get("", response_model=None)
async def list_kbs(
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_QUERY, SCOPE_ADMIN)),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[KbResponse]:
    pool = _require_pool(request)
    rows = await repository.list_kbs(pool, principal.tenant_id, limit=limit, offset=offset)  # type: ignore[arg-type]
    return [KbResponse(**r) for r in rows]


@router.get("/{kb_id}", response_model=None)
async def get_kb(
    kb_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_QUERY, SCOPE_ADMIN)),
) -> KbResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    pool = _require_pool(request)
    kb = await repository.get_kb(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
    if kb is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Knowledge base not found.")
    return KbResponse(**kb)


@router.get("/{kb_id}/status", response_model=None)
async def kb_status(
    kb_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_QUERY, SCOPE_ADMIN)),
) -> KbStatusResponse:
    kb_id = parse_uuid(kb_id, field="kb_id")
    pool = _require_pool(request)
    kb = await repository.get_kb(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
    if kb is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Knowledge base not found.")
    status = await repository.kb_status(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
    return KbStatusResponse(**status)


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_ADMIN)),
) -> None:
    kb_id = parse_uuid(kb_id, field="kb_id")
    pool = _require_pool(request)
    # The platform-skills KB is never deletable via the API.
    settings = get_settings()
    if principal.tenant_id == PLATFORM_TENANT_ID:
        kb = await repository.get_kb(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
        if kb is not None and kb["name"] == settings.bootstrap_kb_name:
            raise ApiError(ErrorCode.FORBIDDEN, "The platform-skills KB cannot be deleted.")
    deleted = await repository.delete_kb(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
    if not deleted:
        raise ApiError(ErrorCode.NOT_FOUND, "Knowledge base not found.")
    logger.info("kb_deleted", kb_id=kb_id, tenant=principal.tenant_id)
