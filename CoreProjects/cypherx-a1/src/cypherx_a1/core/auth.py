"""Inbound agent JWT verification (Contracts 1, 13).

cypherx-a1 is an edge-facing app: callers (the frontend BFF / edge, or an external
api-key-exchanged JWT) submit requests with a bare agent JWT in ``Authorization``.
The service re-verifies that JWT locally against the Auth JWKS (defense in depth — same
posture as xAgent/llms/guardrails/rag) and resolves a :class:`Principal`.

Verification (external bare-JWT mode):
  * signature via JWKS (RS256), ``iss`` == ``auth_issuer_url``, ``aud`` contains
    ``auth_platform_audience``, ``exp`` valid (±60s skew), ``sub`` present.
  * the caller must hold at least ONE allowed scope (else 403). Per-endpoint scope gating
    (``require_scope``) further restricts ingest/admin routes.
  * ``tenant_id`` / ``agent_id`` come ONLY from the JWT (Contract 13) — never the body.

The raw verified bearer is preserved on ``Principal.raw_token`` so handlers forward it
verbatim via ``X-Forwarded-Agent-JWT`` on every downstream SharedCore call (Contract 12).
After signature/claims pass, the token runs through the shared Valkey revocation MIRROR
(fail-open). ``require_principal`` is the FastAPI dependency (overridable in tests).
"""

from __future__ import annotations

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

# Product read scope; platform/admin scopes also admitted so an admin JWT is not 403'd at
# the dependency before an endpoint's own finer check runs.
SCOPE_QUERY = "cypherxa1:query"
SCOPE_INGEST = "cypherxa1:ingest"
SCOPE_ADMIN = "cypherxa1:admin"
_BASE_ALLOWED_SCOPES = frozenset(
    {SCOPE_QUERY, SCOPE_INGEST, SCOPE_ADMIN, "agent:execute", "agent:admin", "platform:admin"}
)
_ADMIN_SCOPES = frozenset({SCOPE_ADMIN, "agent:admin", "platform:admin"})
_CLOCK_SKEW_SECONDS = 60


@dataclass
class Principal:
    """Resolved caller identity for an inbound request."""

    tenant_id: str
    agent_id: str | None
    scopes: list[str]
    principal_type: str = "agent"  # 'agent' | 'api_key'
    api_key_id: str | None = None
    raw_token: str = ""  # verified bearer, forwarded as X-Forwarded-Agent-JWT
    raw_claims: dict[str, Any] = field(default_factory=dict)
    kid: str | None = None

    def has_any(self, scopes: frozenset[str]) -> bool:
        return not scopes.isdisjoint(self.scopes)


_jwks_clients: dict[str, PyJWKClient] = {}


def get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Return a process-cached PyJWKClient (5-min key cache, refresh-on-kid-miss)."""
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
    client = get_jwks_client(settings.auth_jwks_url)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.auth_platform_audience,
            issuer=settings.auth_issuer_url,
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ApiError(ErrorCode.UNAUTHORIZED, f"Invalid token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — JWKS fetch / network failures
        raise ApiError(ErrorCode.UNAUTHORIZED, "Unable to verify token signing key.") from exc


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


def _kid_of(token: str) -> str | None:
    try:
        return jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None


def _resolve_principal(request: Request, settings: Settings) -> Principal:
    token = _bearer(request)
    claims = _decode(token, settings)

    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Agent token missing tenant_id claim.")

    ptype = "api_key" if claims.get("api_key_id") else "agent"
    principal = Principal(
        tenant_id=str(tenant_id),
        agent_id=str(claims["agent_id"]) if claims.get("agent_id") else None,
        scopes=_scopes_of(claims),
        principal_type=ptype,
        api_key_id=str(claims["api_key_id"]) if claims.get("api_key_id") else None,
        raw_token=token,
        raw_claims=claims,
        kid=_kid_of(token),
    )

    if not principal.has_any(_BASE_ALLOWED_SCOPES):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Token missing a required scope (one of: cypherxa1:query/ingest/admin).",
        )
    return principal


async def _enforce_revocation(request: Request, settings: Settings, principal: Principal) -> None:
    """Shared verifier-side revocation MIRROR (Contract 1 / WP03). FAILS OPEN."""
    if not settings.revocation_check_enabled:
        metrics.revocation_checks_total.labels(outcome="disabled").inc()
        return

    state_holder = getattr(getattr(request, "app", None), "state", None)
    valkey = getattr(state_holder, "valkey", None) if state_holder is not None else None
    if valkey is None:
        metrics.revocation_checks_total.labels(outcome="skipped").inc()
        metrics.revocation_check_skipped_total.inc()
        return

    claims = principal.raw_claims or {}
    jti = claims.get("jti")
    iat = claims.get("iat")
    try:
        revoked = await valkey.revocation_lookup(
            prefix=settings.revocation_key_prefix,
            jti=str(jti) if jti else None,
            kid=principal.kid,
            agent_id=principal.agent_id,
            iat=int(iat) if isinstance(iat, int | float) else None,
            timeout_seconds=settings.revocation_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN (availability wins)
        metrics.revocation_checks_total.labels(outcome="skipped").inc()
        metrics.revocation_check_skipped_total.inc()
        logger.warning("revocation_check_skipped", reason="valkey_unavailable", error=str(exc))
        return

    if revoked:
        metrics.revocation_checks_total.labels(outcome="revoked").inc()
        logger.info("token_revoked", agent_id=principal.agent_id, tenant_id=principal.tenant_id)
        raise ApiError(ErrorCode.TOKEN_REVOKED, "Token has been revoked.")
    metrics.revocation_checks_total.labels(outcome="clean").inc()


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: verify inbound agent JWT (+ revocation mirror), return Principal."""
    settings = get_settings()
    principal = _resolve_principal(request, settings)
    await _enforce_revocation(request, settings, principal)
    return principal


def require_scope(principal: Principal, scopes: frozenset[str], action: str) -> None:
    """Raise 403 unless the principal holds at least one of ``scopes``."""
    if not principal.has_any(scopes):
        raise ApiError(ErrorCode.FORBIDDEN, f"Token missing a required scope for {action}.")


def admin_scopes() -> frozenset[str]:
    return _ADMIN_SCOPES


def ingest_scopes() -> frozenset[str]:
    return frozenset({SCOPE_INGEST}) | _ADMIN_SCOPES


def query_scopes() -> frozenset[str]:
    return frozenset({SCOPE_QUERY}) | ingest_scopes()
