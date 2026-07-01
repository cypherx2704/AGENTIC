"""KB ACL management (Component 5c) — list/replace/add/remove ACL rows.

All four endpoints require the ``rag:admin`` scope AND that the caller holds the ``admin``
permission on the KB (ACL-on-ACL). The default ``(tenant,'*')`` row is created on KB create;
these endpoints let a tenant restrict / extend access (e.g. an external chat-app vendor
adding per-end-user ``principal_type='user'`` rows).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request

from ..core.auth import SCOPE_ADMIN, Principal, require_scope
from ..core.errors import ApiError, ErrorCode, parse_uuid
from ..db import repository
from ..db.pool import in_tenant
from ..models.api import (
    AclListResponse,
    AclResponse,
    AclRow,
    ReplaceAclsRequest,
)
from ..services import acl
from ..services.acl import OP_ADMIN

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/kbs", tags=["acls"])


def _require_pool(request: Request) -> object:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Database is not available.")
    return pool


def _iso(value: object) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z") if hasattr(value, "isoformat") else str(value)


async def _ensure_kb_admin(pool: object, principal: Principal, kb_id: str, settings: object) -> str:
    kb_id = parse_uuid(kb_id, field="kb_id")
    kb = await repository.get_kb(pool, principal.tenant_id, kb_id)  # type: ignore[arg-type]
    if kb is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Knowledge base not found.")
    await acl.check_access(pool, principal, kb_id, OP_ADMIN, settings=settings)  # type: ignore[arg-type]
    return kb_id


@router.get("/{kb_id}/acls", response_model=None)
async def list_acls(
    kb_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_ADMIN)),
) -> AclListResponse:
    settings = request.app.state.settings
    pool = _require_pool(request)
    kb_id = await _ensure_kb_admin(pool, principal, kb_id, settings)

    from psycopg.rows import dict_row

    async def _txn(conn: object) -> list[dict]:
        cur = conn.cursor(row_factory=dict_row)  # type: ignore[attr-defined]
        await cur.execute("SELECT * FROM rag.kb_acls WHERE kb_id = %s", (kb_id,))
        return await cur.fetchall()

    rows = await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
    return AclListResponse(
        acls=[
            AclResponse(
                kb_id=str(r["kb_id"]),
                principal_type=r["principal_type"],
                principal_id=r["principal_id"],
                permissions=list(r["permissions"]),
                created_by=str(r["created_by"]),
                created_at=_iso(r["created_at"]) or "",
                expires_at=_iso(r["expires_at"]),
            )
            for r in rows
        ]
    )


@router.post("/{kb_id}/acls", response_model=None, status_code=201)
async def add_acl(
    kb_id: str,
    body: AclRow,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_ADMIN)),
) -> dict:
    settings = request.app.state.settings
    pool = _require_pool(request)
    kb_id = await _ensure_kb_admin(pool, principal, kb_id, settings)

    async def _txn(conn: object) -> None:
        await conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO rag.kb_acls
              (kb_id, tenant_id, principal_type, principal_id, permissions, created_by, expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (kb_id, principal_type, principal_id)
            DO UPDATE SET permissions = EXCLUDED.permissions, expires_at = EXCLUDED.expires_at
            """,
            (
                kb_id, principal.tenant_id, body.principal_type, body.principal_id,
                body.permissions, principal.agent_id or principal.tenant_id, body.expires_at,
            ),
        )

    await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
    logger.info("kb_acl_added", kb_id=kb_id, principal_type=body.principal_type)
    return {"status": "ok"}


@router.put("/{kb_id}/acls", response_model=None)
async def replace_acls(
    kb_id: str,
    body: ReplaceAclsRequest,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_ADMIN)),
) -> dict:
    settings = request.app.state.settings
    pool = _require_pool(request)
    kb_id = await _ensure_kb_admin(pool, principal, kb_id, settings)

    async def _txn(conn: object) -> None:
        await conn.execute("DELETE FROM rag.kb_acls WHERE kb_id = %s", (kb_id,))  # type: ignore[attr-defined]
        for row in body.acls:
            await conn.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO rag.kb_acls
                  (kb_id, tenant_id, principal_type, principal_id, permissions, created_by, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    kb_id, principal.tenant_id, row.principal_type, row.principal_id,
                    row.permissions, principal.agent_id or principal.tenant_id, row.expires_at,
                ),
            )

    await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
    logger.info("kb_acls_replaced", kb_id=kb_id, count=len(body.acls))
    return {"status": "ok", "count": len(body.acls)}


@router.delete("/{kb_id}/acls/{principal_type}/{principal_id}", status_code=204)
async def remove_acl(
    kb_id: str,
    principal_type: str,
    principal_id: str,
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_ADMIN)),
) -> None:
    settings = request.app.state.settings
    pool = _require_pool(request)
    kb_id = await _ensure_kb_admin(pool, principal, kb_id, settings)

    async def _txn(conn: object) -> int:
        cur = await conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM rag.kb_acls WHERE kb_id = %s AND principal_type = %s AND principal_id = %s",
            (kb_id, principal_type, principal_id),
        )
        return cur.rowcount

    removed = await in_tenant(pool, principal.tenant_id, _txn)  # type: ignore[arg-type]
    if not removed:
        raise ApiError(ErrorCode.NOT_FOUND, "ACL row not found.")
