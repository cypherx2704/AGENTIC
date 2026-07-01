"""BYOK key management — POST/GET/DELETE /v1/keys (+ rotate). WP06.

A tenant registers their own provider API key so their LLM traffic bills to their
upstream account. The raw secret is sealed (``sealed:v1:`` AES-256-GCM envelope, see
``services/byok.py``) BEFORE it touches the DB and is NEVER returned by any endpoint
or written to a log — responses carry only the ``key_id`` + non-secret metadata.

Endpoints (prefix ``/v1``; tenant taken ONLY from the JWT Principal — Contract 13):

* ``POST   /v1/keys``            register a key (status='active').
* ``POST   /v1/keys/{id}/rotate`` insert a new active key, flip the old one to
  'rotating' with ``grace_until = now + byok_grace_days`` (both valid during grace).
* ``GET    /v1/keys``            list the tenant's keys (no secrets).
* ``DELETE /v1/keys/{id}``       revoke (status='revoked'; soft delete, kept for audit).

AuthZ: ``llm:invoke`` (the gateway-wide required scope, enforced by ``require_principal``)
PLUS ``tenant:admin`` OR ``platform:admin`` — key management is an admin operation, so a
plain invoke-only agent token is rejected 403. All writes run under RLS via ``in_tenant``.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from ..core.auth import Principal, require_principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant
from ..services import byok

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["keys"])

# Admin scopes that may manage BYOK keys (either is sufficient).
_ADMIN_SCOPES = ("tenant:admin", "platform:admin")


# ── request/response models ──────────────────────────────────────────────────────
class RegisterKeyRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    secret: str = Field(min_length=1)
    priority: int = 100
    # OpenAI-compatible base URL for this connection (OpenRouter, Together, Groq, vLLM,
    # Ollama, self-hosted, …). Omit for native OpenAI/Anthropic.
    base_url: str | None = Field(default=None, max_length=500)
    # Wire protocol: 'openai_compatible' (default) or 'anthropic'. Auto-derived from the
    # provider name when omitted (anthropic -> 'anthropic', else 'openai_compatible').
    kind: str | None = Field(default=None)
    # Optional friendly name shown in the UI.
    label: str | None = Field(default=None, max_length=100)


class RotateKeyRequest(BaseModel):
    secret: str = Field(min_length=1)
    priority: int | None = None


# ── helpers ────────────────────────────────────────────────────────────────────
def _require_admin(principal: Principal) -> None:
    """403 unless the caller carries a BYOK-management admin scope."""
    if not any(scope in principal.scopes for scope in _ADMIN_SCOPES):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "BYOK key management requires the 'tenant:admin' or 'platform:admin' scope.",
            details={"required_any": list(_ADMIN_SCOPES)},
        )


def _require_uuid(key_id: str) -> None:
    """404 for a non-UUID path id BEFORE it reaches psycopg.

    ``key_id`` is a free-form path string; binding a non-UUID value into a ``uuid``
    column makes psycopg raise ``invalid input syntax for type uuid``, which would
    bubble up as a generic 500. A malformed id can never name a real row, so treat it
    as NOT_FOUND (same shape as the "key not found" miss below).
    """
    try:
        uuid.UUID(key_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ApiError(
            ErrorCode.NOT_FOUND, "BYOK key not found.", details={"key_id": key_id}
        ) from exc


def _get_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        # No DB wired (e.g. a minimal/unit test app) — keys cannot be persisted.
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Key store is not available.")
    return pool


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """Project a tenant_provider_keys row to its NON-SECRET public shape."""
    grace = row.get("grace_until")
    return {
        "key_id": str(row["key_id"]),
        "provider": row["provider"],
        "priority": row["priority"],
        "status": row["status"],
        "grace_until": grace.isoformat() if grace is not None else None,
        "base_url": row.get("base_url"),
        "kind": row.get("kind"),
        "label": row.get("label"),
    }


# ── POST /v1/keys ────────────────────────────────────────────────────────────────
@router.post("/keys", status_code=201, response_model=None)
async def register_key(
    body: RegisterKeyRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Register a tenant BYOK key. The secret is sealed before insert; never returned."""
    _require_admin(principal)
    settings = _settings(request)
    pool = _get_pool(request)

    if not byok.is_enabled(settings):
        # No KEK configured -> we cannot seal a secret at rest. Refuse rather than store
        # plaintext. (Platform keys still serve traffic; BYOK registration is unavailable.)
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "BYOK is not enabled on this gateway (no key-encryption-key configured).",
        )

    try:
        secret_ref = byok.seal(body.secret, settings)
    except byok.ByokDisabledError as exc:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "BYOK is not enabled on this gateway (no key-encryption-key configured).",
        ) from exc

    # Derive the wire protocol when the caller didn't specify it.
    kind = (
        body.kind
        or ("anthropic" if body.provider.lower() == "anthropic" else "openai_compatible")
    ).lower()

    sql = """
        INSERT INTO llms.tenant_provider_keys
            (tenant_id, provider, secret_ref, priority, status, base_url, kind, label)
        VALUES (%s, %s, %s, %s, 'active', %s, %s, %s)
        RETURNING key_id, provider, priority, status, grace_until
    """

    async def _insert(conn: Any) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            sql,
            (
                principal.tenant_id, body.provider, secret_ref, body.priority,
                body.base_url, kind, body.label,
            ),
        )
        return await cur.fetchone()

    try:
        row = await in_tenant(pool, principal.tenant_id, _insert)
    except Exception as exc:  # noqa: BLE001 — surface a clean 503 (e.g. unknown provider FK)
        logger.warning("byok_register_failed", provider=body.provider, error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Could not register the BYOK key (provider unknown or store unavailable).",
            details={"provider": body.provider},
        ) from exc

    logger.info("byok_key_registered", provider=body.provider, key_id=str(row["key_id"]))
    return _serialise(row)


# ── POST /v1/keys/{id}/rotate ──────────────────────────────────────────────────────
@router.post("/keys/{key_id}/rotate", response_model=None)
async def rotate_key(
    key_id: str,
    body: RotateKeyRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Rotate a key: insert the new sealed secret (active), put the OLD key into a grace
    window (status='rotating', grace_until = now + byok_grace_days). Both are selectable
    during grace so in-flight callers keyed to the previous upstream secret keep working.
    """
    _require_admin(principal)
    _require_uuid(key_id)
    settings = _settings(request)
    pool = _get_pool(request)

    if not byok.is_enabled(settings):
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "BYOK is not enabled on this gateway (no key-encryption-key configured).",
        )

    try:
        new_ref = byok.seal(body.secret, settings)
    except byok.ByokDisabledError as exc:
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "BYOK is not enabled on this gateway (no key-encryption-key configured).",
        ) from exc

    grace_days = settings.byok_grace_days

    # Look up the OLD key (RLS scopes it to the caller's tenant), flip it to 'rotating' with
    # a grace window, and insert the NEW active key inheriting the old provider/priority
    # (unless the caller overrode priority) — all in ONE tenant transaction.
    async def _rotate(conn: Any) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT key_id, provider, priority, status FROM llms.tenant_provider_keys "
            "WHERE key_id = %s",
            (key_id,),
        )
        old = await cur.fetchone()
        if old is None or old["status"] == "revoked":
            raise ApiError(ErrorCode.NOT_FOUND, "BYOK key not found.", details={"key_id": key_id})

        await conn.execute(
            "UPDATE llms.tenant_provider_keys "
            "SET status = 'rotating', grace_until = NOW() + (%s * INTERVAL '1 day') "
            "WHERE key_id = %s",
            (grace_days, key_id),
        )
        new_priority = body.priority if body.priority is not None else old["priority"]
        ins = await conn.cursor(row_factory=dict_row).execute(
            "INSERT INTO llms.tenant_provider_keys "
            "(tenant_id, provider, secret_ref, priority, status) "
            "VALUES (%s, %s, %s, %s, 'active') "
            "RETURNING key_id, provider, priority, status, grace_until",
            (principal.tenant_id, old["provider"], new_ref, new_priority),
        )
        return await ins.fetchone()

    try:
        row = await in_tenant(pool, principal.tenant_id, _rotate)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 — store unavailable / unexpected
        logger.warning("byok_rotate_failed", key_id=key_id, error=str(exc))
        raise ApiError(
            ErrorCode.SERVICE_UNAVAILABLE, "Could not rotate the BYOK key."
        ) from exc

    logger.info(
        "byok_key_rotated",
        old_key_id=key_id,
        new_key_id=str(row["key_id"]),
        provider=row["provider"],
        grace_days=grace_days,
    )
    return {"rotated_from": key_id, **_serialise(row)}


# ── GET /v1/keys ──────────────────────────────────────────────────────────────────
@router.get("/keys", response_model=None)
async def list_keys(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, list[dict[str, Any]]]:
    """List the caller tenant's BYOK keys (id/provider/priority/status/grace_until). No secrets."""
    _require_admin(principal)
    pool = _get_pool(request)

    sql = (
        "SELECT key_id, provider, priority, status, grace_until, base_url, kind, label "
        "FROM llms.tenant_provider_keys "
        "ORDER BY provider ASC, priority ASC, created_at DESC"
    )

    async def _list(conn: Any) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(sql)
        return await cur.fetchall()

    rows = await in_tenant(pool, principal.tenant_id, _list)
    return {"data": [_serialise(r) for r in rows]}


# ── DELETE /v1/keys/{id} ────────────────────────────────────────────────────────────
@router.delete("/keys/{key_id}", response_model=None)
async def revoke_key(
    key_id: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Revoke a key (status='revoked'). Soft delete — the row is retained for audit and is
    never selected by the resolver again."""
    _require_admin(principal)
    _require_uuid(key_id)
    pool = _get_pool(request)

    sql = (
        "UPDATE llms.tenant_provider_keys SET status = 'revoked' "
        "WHERE key_id = %s AND status != 'revoked' "
        "RETURNING key_id, provider, priority, status, grace_until"
    )

    async def _revoke(conn: Any) -> dict[str, Any] | None:
        cur = await conn.cursor(row_factory=dict_row).execute(sql, (key_id,))
        return await cur.fetchone()

    row = await in_tenant(pool, principal.tenant_id, _revoke)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "BYOK key not found.", details={"key_id": key_id})

    logger.info("byok_key_revoked", key_id=key_id, provider=row["provider"])
    return _serialise(row)
