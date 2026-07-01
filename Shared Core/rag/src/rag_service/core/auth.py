"""Dual-mode agent/service JWT verification (Contracts 1, 12, 13).

Two accepted authentication modes, both feeding the SAME downstream Principal:

* **EXTERNAL** — bare agent (or api-key-exchanged) JWT in ``Authorization``. The
  bearer's ``sub`` is the agent_id; it carries ``tenant_id`` / ``agent_id`` /
  ``scopes`` directly. No ``X-Forwarded-Agent-JWT``.
* **INTERNAL** — service JWT (``sub`` = ``svc:*``) in ``Authorization`` PLUS an
  ``X-Forwarded-Agent-JWT`` header carrying the agent JWT. BOTH are verified and the
  service token's ``on_behalf_of`` MUST equal the forwarded agent JWT's ``agent_id``
  (Contract 12) — 401 on mismatch.

In both modes: ``iss`` must equal ``auth_issuer_url``, ``aud`` must contain
``auth_platform_audience`` (or the service-token wildcard ``*``), ``exp`` must be valid.
``tenant_id`` / ``agent_id`` are taken ONLY from the JWT (Contract 13) — never from the
request body. Per-endpoint scope enforcement is done by the route via ``require_scope``;
``require_principal`` only authenticates.

After verification the shared WP03 verifier-side revocation mirror runs (fail-open).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jwt
import structlog
from fastapi import Request
from jwt import PyJWKClient

from . import metrics
from .config import Settings, get_settings
from .errors import ApiError, ErrorCode

logger = structlog.get_logger(__name__)

_CLOCK_SKEW_SECONDS = 60

# RAG scopes (per the Component 5c API table). A query needs rag:query; ingest needs
# rag:ingest; KB CRUD + ACL management need rag:admin. The well-known internal-read
# scope used for cross-tenant platform reads (Skills -> RAG) also grants query.
SCOPE_QUERY = "rag:query"
SCOPE_INGEST = "rag:ingest"
SCOPE_ADMIN = "rag:admin"
SCOPE_INTERNAL_READ = "internal:read"


@dataclass
class Principal:
    """Resolved caller identity for a request."""

    tenant_id: str
    agent_id: str | None
    scopes: list[str]
    principal_type: str  # 'agent' | 'api_key' | 'service' | 'on_behalf_of_user'
    api_key_id: str | None = None
    user_id: str | None = None  # opaque cypherx:user_id claim, for principal_type='user' ACLs
    on_behalf_of: str | None = None
    raw_claims: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _RevocationSubject:
    label: str
    jti: str | None
    kid: str | None
    agent_id: str | None
    iat: int | None


_jwks_clients: dict[str, PyJWKClient] = {}


def get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Return a process-cached PyJWKClient for the given JWKS URL."""
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        _jwks_clients[jwks_url] = client
    return client


def warm_jwks(settings: Settings) -> None:
    """Best-effort pre-fetch of the JWKS document at startup."""
    try:
        get_jwks_client(settings.auth_jwks_url).get_signing_keys()
        logger.info("jwks_warmed", jwks_url=settings.auth_jwks_url)
    except Exception as exc:  # noqa: BLE001 — warming is best-effort
        logger.warning("jwks_warm_failed", error=str(exc))


def _decode(token: str, settings: Settings) -> dict[str, Any]:
    """Verify a JWT's signature + iss/aud/exp and return its claims."""
    client = get_jwks_client(settings.auth_jwks_url)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            # Accept the platform audience OR the service-token wildcard "*" (Contract 12).
            audience=[settings.auth_platform_audience, "*"],
            issuer=settings.auth_issuer_url,
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ApiError(ErrorCode.UNAUTHORIZED, f"Invalid token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — JWKS fetch / network failures
        raise ApiError(ErrorCode.UNAUTHORIZED, "Unable to verify token signing key.") from exc


def _kid_of(token: str) -> str | None:
    try:
        return jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None


def _revocation_subject(label: str, token: str, claims: dict[str, Any]) -> _RevocationSubject:
    iat = claims.get("iat")
    return _RevocationSubject(
        label=label,
        jti=str(claims["jti"]) if claims.get("jti") else None,
        kid=_kid_of(token),
        agent_id=str(claims["agent_id"]) if claims.get("agent_id") else None,
        iat=int(iat) if isinstance(iat, int | float) else None,
    )


def _bearer(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Missing or malformed Authorization header.")
    return token.strip()


def _scopes_of(claims: dict[str, Any]) -> list[str]:
    scopes = claims.get("scopes", [])
    if isinstance(scopes, str):
        return scopes.split()
    if isinstance(scopes, list):
        return [str(s) for s in scopes]
    return []


def _principal_from_agent_claims(
    claims: dict[str, Any], *, principal_type: str, on_behalf_of: str | None = None
) -> Principal:
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Agent token missing tenant_id claim.")
    return Principal(
        tenant_id=str(tenant_id),
        agent_id=str(claims["agent_id"]) if claims.get("agent_id") else None,
        scopes=_scopes_of(claims),
        principal_type=principal_type,
        api_key_id=str(claims["api_key_id"]) if claims.get("api_key_id") else None,
        user_id=str(claims["cypherx:user_id"]) if claims.get("cypherx:user_id") else None,
        on_behalf_of=on_behalf_of,
        raw_claims=claims,
    )


def _principal_from_service_claims(claims: dict[str, Any]) -> Principal:
    """Build a principal for a service token that carries its OWN tenant_id (the
    cross-tenant platform-read pattern: Skills mints a token with the platform tenant).

    Used when a service JWT arrives WITHOUT an X-Forwarded-Agent-JWT but carries a
    tenant_id + an on_behalf_of audit field + the internal:read scope.
    """
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Service token missing tenant_id claim.")
    return Principal(
        tenant_id=str(tenant_id),
        agent_id=None,
        scopes=_scopes_of(claims),
        principal_type="service",
        on_behalf_of=str(claims["on_behalf_of"]) if claims.get("on_behalf_of") else None,
        raw_claims=claims,
    )


def _resolve_principal(
    request: Request, settings: Settings
) -> tuple[Principal, list[_RevocationSubject]]:
    bearer_token = _bearer(request)
    bearer_claims = _decode(bearer_token, settings)
    sub = str(bearer_claims.get("sub", ""))
    forwarded = request.headers.get("x-forwarded-agent-jwt")
    subjects: list[_RevocationSubject] = []

    if sub.startswith("svc:") or sub.startswith("svc-ext:"):
        if forwarded:
            # ── INTERNAL mode: service token + forwarded agent JWT ───────────────
            agent_claims = _decode(forwarded, settings)
            agent_id = agent_claims.get("agent_id")
            on_behalf_of = bearer_claims.get("on_behalf_of")
            if not on_behalf_of or on_behalf_of != agent_id:
                raise ApiError(
                    ErrorCode.UNAUTHORIZED,
                    "Service token on_behalf_of does not match forwarded agent JWT agent_id.",
                )
            principal = _principal_from_agent_claims(
                agent_claims, principal_type="service", on_behalf_of=str(on_behalf_of)
            )
            subjects.append(_revocation_subject("service", bearer_token, bearer_claims))
            subjects.append(_revocation_subject("forwarded_agent", forwarded, agent_claims))
        else:
            # ── CROSS-TENANT PLATFORM-READ mode (Skills -> RAG): a service token that
            # carries its own tenant_id (the platform tenant) + on_behalf_of. RLS uses the
            # token's tenant_id; the ACL/audit trail keeps on_behalf_of. ───────────────
            principal = _principal_from_service_claims(bearer_claims)
            subjects.append(_revocation_subject("service", bearer_token, bearer_claims))
    elif forwarded:
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            "X-Forwarded-Agent-JWT is only valid with a service-token Authorization.",
        )
    else:
        # ── EXTERNAL mode: bare agent / api-key JWT ───────────────────────────
        ptype = "api_key" if bearer_claims.get("api_key_id") else "agent"
        principal = _principal_from_agent_claims(bearer_claims, principal_type=ptype)
        subjects.append(_revocation_subject("bearer", bearer_token, bearer_claims))

    return principal, subjects


async def _enforce_revocation(
    request: Request, settings: Settings, subjects: list[_RevocationSubject]
) -> None:
    """Shared verifier-side revocation MIRROR (WP03). FAILS OPEN on Valkey unavailable."""
    if not settings.revocation_check_enabled:
        return
    valkey = getattr(request.app.state, "valkey", None)
    if valkey is None:
        logger.info("revocation_check_skipped", reason="no_valkey_client", skipped=True)
        metrics.revocation_check_skipped_total.inc()
        return

    prefix = settings.revocation_key_prefix
    timeout = settings.revocation_valkey_timeout_seconds
    try:
        for subj in subjects:
            if subj.jti and await valkey.get(f"{prefix}jti:{subj.jti}", timeout_seconds=timeout):
                _reject_revoked("jti", subj)
            if subj.kid and await valkey.get(f"{prefix}kid:{subj.kid}", timeout_seconds=timeout):
                _reject_revoked("kid", subj)
            if subj.agent_id and subj.iat is not None:
                epoch = await valkey.get(f"{prefix}agent:{subj.agent_id}", timeout_seconds=timeout)
                if epoch is not None and subj.iat < _to_epoch(epoch):
                    _reject_revoked("agent", subj)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN
        logger.warning(
            "revocation_check_skipped", reason="valkey_unavailable", error=str(exc), skipped=True
        )
        metrics.revocation_check_skipped_total.inc()


def _to_epoch(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _reject_revoked(rule: str, subj: _RevocationSubject) -> None:
    logger.info("token_revoked", rule=rule, token=subj.label, agent_id=subj.agent_id)
    metrics.revocation_rejected_total.labels(rule).inc()
    raise ApiError(ErrorCode.TOKEN_REVOKED, "Token has been revoked.")


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: verify auth and return the resolved Principal."""
    settings = get_settings()
    principal, subjects = _resolve_principal(request, settings)
    await _enforce_revocation(request, settings, subjects)
    return principal


def require_scope(*needed: str) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency factory enforcing that the principal carries ANY of ``needed``.

    Returns 403 FORBIDDEN when none of the required scopes are present. ``internal:read``
    always satisfies a read scope requirement (the cross-tenant platform-read token).
    """
    required = set(needed)

    async def _dep(request: Request) -> Principal:
        principal = await require_principal(request)
        held = set(principal.scopes)
        if required & held:
            return principal
        # internal:read satisfies query-only requirements.
        if SCOPE_QUERY in required and SCOPE_INTERNAL_READ in held:
            return principal
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"Token missing a required scope ({', '.join(sorted(required))}).",
        )

    return _dep
