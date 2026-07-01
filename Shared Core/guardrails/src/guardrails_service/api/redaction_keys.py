"""POST /v1/redaction-keys/rotate — rotate a tenant's redaction HMAC key (WP07).

Tenant-admin only (scope ``tenant:admin``). Rotation mints a new ``current`` key and
demotes the prior current to ``retired`` with a 30-day grace (``redaction_key_grace_days``)
so tokens minted just before rotation still resolve; a lifespan-scheduled retirement job
hard-retires keys past grace.

With no DB pool configured (local/unit) rotation cannot persist a row, so the endpoint
returns 503 SERVICE_UNAVAILABLE — there is nothing to rotate against. This keeps the
hot-path resolver's keyless fallback (platform key) intact without pretending a rotation
happened.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from ..core.auth import Principal, require_principal
from ..core.errors import ApiError, ErrorCode
from ..services.redaction_keys import rotate_key

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["redaction-keys"])

REQUIRED_ADMIN_SCOPE = "tenant:admin"


def require_tenant_admin(principal: Principal = Depends(require_principal)) -> Principal:
    """Dependency: the caller must additionally carry the ``tenant:admin`` scope (403)."""
    if REQUIRED_ADMIN_SCOPE not in principal.scopes:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"Token missing required scope '{REQUIRED_ADMIN_SCOPE}'.",
        )
    return principal


@router.post("/redaction-keys/rotate")
async def rotate_redaction_key(
    request: Request,
    principal: Principal = Depends(require_tenant_admin),
) -> dict[str, Any]:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Redaction-key rotation requires a configured database.",
            status_code=503,
        )

    # Optional BYO key_ref in the body (env: / sealed:). Absent => a generated env: ref.
    key_ref: str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_ref = body.get("key_ref")
            if isinstance(raw_ref, str) and raw_ref:
                if not (raw_ref.startswith("env:") or raw_ref.startswith("sealed:")):
                    raise ApiError(
                        ErrorCode.VALIDATION_ERROR,
                        "key_ref must use a supported scheme ('env:' or 'sealed:').",
                        status_code=400,
                    )
                key_ref = raw_ref
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 — empty/non-JSON body is fine (no BYO ref)
        key_ref = None

    try:
        result = await rotate_key(pool, principal.tenant_id, key_ref=key_ref)
    except Exception as exc:  # noqa: BLE001 — surface a clean 503 rather than a 500
        logger.error("redaction_key_rotate_failed", error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Could not rotate the redaction key.",
            status_code=503,
        ) from exc

    # Drop the cached key so the next check picks up the rotation immediately.
    resolver = getattr(request.app.state, "redaction_resolver", None)
    if resolver is not None and hasattr(resolver, "invalidate"):
        resolver.invalidate(principal.tenant_id)

    return {
        "rotated": True,
        "tenant_id": principal.tenant_id,
        "key_id": result["key_id"],
        "key_ref": result["key_ref"],
        "status": result["status"],
        "grace_days": request.app.state.settings.redaction_key_grace_days,
    }
