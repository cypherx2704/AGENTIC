"""Inbound agent JWT verification (Contracts 1, 13).

xAgent is an EDGE service: clients (via Kong) submit tasks with a bare agent JWT in
the ``Authorization`` header. xAgent re-verifies that JWT locally against the Auth
JWKS (defense in depth — Phase 9A step 2) and resolves a :class:`Principal`. There is
NO ``X-Forwarded-Agent-JWT`` inbound mode here (that is the role xAgent plays when it
calls *downstream*; see ``services/service_token.py`` + the downstream clients).

Verification rules (external bare-JWT mode):
  * signature via JWKS (RS256), ``iss`` == ``auth_issuer_url``,
    ``aud`` contains ``auth_platform_audience``, ``exp`` valid (±60s skew).
  * scope ``agent:execute`` REQUIRED (403 FORBIDDEN otherwise).
  * ``tenant_id`` / ``agent_id`` are taken ONLY from the JWT (Contract 13) — never
    from the request body.

The raw verified bearer token is preserved on ``Principal.raw_token`` so the task
handler can forward it verbatim via ``X-Forwarded-Agent-JWT`` on every downstream
call (Phase 9 forwarding rule).

After signature/iss/aud/exp/scope pass, the inbound token is run through the SHARED
live-revocation MIRROR (Component 3c, WP03): the same Valkey kill-switch keys Auth
writes (``<prefix>jti:``, ``<prefix>kid:``, ``<prefix>agent:``) are read, and a hit
rejects with 401 ``TOKEN_REVOKED``. The check FAILS OPEN — a Valkey outage accepts the
token (+ logs + a metric) so the kill-switch never becomes an availability risk.

``require_principal`` is the FastAPI dependency. Tests override it via
``app.dependency_overrides[require_principal]`` to inject a fixed Principal — no real
Auth / JWKS needed under test (the same seam llms-gateway + guardrails use).
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

REQUIRED_SCOPE = "agent:execute"
# A valid inbound caller holds agent:execute (normal agents running tasks) OR an admin scope
# (admins reaching admin-only endpoints like POST /v1/agents/{id}/runtime, which further gates on
# its own _ADMIN_SCOPES). Without admin here, a platform:admin JWT is wrongly 403'd at the
# dependency before the endpoint's own check runs.
_BASE_ALLOWED_SCOPES = frozenset({"agent:execute", "agent:admin", "platform:admin"})
_CLOCK_SKEW_SECONDS = 60


@dataclass
class Principal:
    """Resolved caller identity for an inbound task request."""

    tenant_id: str
    agent_id: str | None
    scopes: list[str]
    principal_type: str = "agent"  # 'agent' | 'api_key'
    api_key_id: str | None = None
    raw_token: str = ""  # the verified bearer, forwarded as X-Forwarded-Agent-JWT
    raw_claims: dict[str, Any] = field(default_factory=dict)
    kid: str | None = None  # signing-key id from the JWT header (revocation kid check)
    # Orchestrator hierarchy (Contract 1 optional claims; forward-compatible). 'orchestrator' |
    # 'sub_agent' | 'user_created'. parent_orchestrator_id is set for sub-agents.
    agent_type: str = "user_created"
    parent_orchestrator_id: str | None = None


# ── JWKS client cache ──────────────────────────────────────────────────────────
# PyJWKClient caches signing keys in-process and refreshes on kid-miss (rate-limited
# internally). One client per JWKS URL.
_jwks_clients: dict[str, PyJWKClient] = {}


def get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Return a process-cached PyJWKClient for the given JWKS URL."""
    client = _jwks_clients.get(jwks_url)
    if client is None:
        # 5-minute key cache (Contract 1); lifespan matches the cluster TTL.
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
            audience=settings.auth_platform_audience,
            issuer=settings.auth_issuer_url,
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ApiError(ErrorCode.UNAUTHORIZED, f"Invalid token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — JWKS fetch / network failures
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            "Unable to verify token signing key.",
        ) from exc


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
    """Signing-key id from the JWT header (trusted post-``_decode``); None if unparseable."""
    try:
        return jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None


def _resolve_principal(request: Request, settings: Settings) -> Principal:
    """Pure resolution logic; the FastAPI dependency is a thin async wrapper."""
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
        agent_type=str(claims.get("agent_type") or "user_created"),
        parent_orchestrator_id=(
            str(claims["parent_orchestrator_id"]) if claims.get("parent_orchestrator_id") else None
        ),
    )

    if _BASE_ALLOWED_SCOPES.isdisjoint(principal.scopes):
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "Token missing a required scope (one of: agent:execute, agent:admin, platform:admin).",
        )
    return principal


async def _enforce_revocation(request: Request, settings: Settings, principal: Principal) -> None:
    """Shared verifier-side revocation MIRROR (Component 3c, WP03).

    AFTER signature/iss/aud/exp/scope pass, reject (401 ``TOKEN_REVOKED``) if ANY shared
    Valkey kill-switch key indicates revocation: ``<prefix>jti:{jti}`` /
    ``<prefix>kid:{kid}`` present, or ``<prefix>agent:{agent_id}`` epoch newer than the
    token's ``iat``. FAIL-OPEN: a missing client or any Valkey error/timeout ACCEPTS the
    token (+log +metric) — revocation is defense-in-depth, so availability wins.
    """
    if not settings.revocation_check_enabled:
        metrics.revocation_checks_total.labels(outcome="disabled").inc()
        return

    valkey = getattr(getattr(request, "app", None), "state", None)
    valkey = getattr(valkey, "valkey", None) if valkey is not None else None
    if valkey is None:
        metrics.revocation_checks_total.labels(outcome="skipped").inc()
        metrics.revocation_check_skipped_total.inc()
        logger.info("revocation_check_skipped", reason="no_valkey_client", skipped=True)
        return

    claims = principal.raw_claims or {}
    jti = claims.get("jti")
    iat = claims.get("iat")
    try:
        state = await valkey.revocation_lookup(
            prefix=settings.revocation_key_prefix,
            jti=str(jti) if jti else None,
            kid=principal.kid,
            agent_id=principal.agent_id,
            timeout_seconds=settings.revocation_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN (availability wins)
        metrics.revocation_checks_total.labels(outcome="skipped").inc()
        metrics.revocation_check_skipped_total.inc()
        logger.warning(
            "revocation_check_skipped", reason="valkey_unavailable", error=str(exc), skipped=True
        )
        return

    if state.jti_revoked:
        _reject_revoked("jti", principal)
    if state.kid_revoked:
        _reject_revoked("kid", principal)
    if state.agent_epoch is not None and isinstance(iat, int | float) and int(iat) < state.agent_epoch:
        _reject_revoked("agent", principal)
    metrics.revocation_checks_total.labels(outcome="clean").inc()


def _reject_revoked(rule: str, principal: Principal) -> None:
    metrics.revocation_checks_total.labels(outcome="revoked").inc()
    logger.info("token_revoked", rule=rule, agent_id=principal.agent_id, tenant_id=principal.tenant_id)
    raise ApiError(ErrorCode.TOKEN_REVOKED, "Token has been revoked.")


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: verify the inbound agent JWT (+ revocation mirror), return the Principal."""
    settings = get_settings()
    principal = _resolve_principal(request, settings)
    await _enforce_revocation(request, settings, principal)
    return principal
