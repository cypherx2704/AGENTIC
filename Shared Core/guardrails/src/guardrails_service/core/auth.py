"""Dual-mode agent/service JWT verification (Contracts 1, 12, 13).

Two accepted authentication modes, both feeding the SAME downstream Principal:

* **EXTERNAL** — bare agent (or api-key-exchanged) JWT in ``Authorization``. The
  bearer's ``sub`` is the agent_id; it carries ``tenant_id`` / ``agent_id`` /
  ``scopes`` directly. No ``X-Forwarded-Agent-JWT``.
* **INTERNAL** — service JWT (``sub`` = ``svc:*``) in ``Authorization`` PLUS an
  ``X-Forwarded-Agent-JWT`` header carrying the agent JWT. BOTH are verified and
  the service token's ``on_behalf_of`` MUST equal the forwarded agent JWT's
  ``agent_id`` (Contract 12) — 401 on mismatch. This is the first-cycle path:
  xAgent calls guardrails with its service token + the forwarded agent JWT.

In both modes: ``iss`` must equal ``auth_issuer_url``, ``aud`` must contain
``auth_platform_audience``, ``exp`` must be valid, and scope ``guardrails:check`` is
required (403 otherwise). ``tenant_id`` / ``agent_id`` are taken ONLY from the JWT
(Contract 13) — never from the request body.

``require_principal`` is the FastAPI dependency; tests override it via
``app.dependency_overrides[require_principal]`` to inject a fixed Principal (the same
seam the llms-gateway uses), so no real Auth / JWKS is needed under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jwt
import structlog
from fastapi import Request
from jwt import PyJWKClient

from . import metrics
from .config import Settings, get_settings
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from .valkey import ValkeyClient

logger = structlog.get_logger(__name__)

REQUIRED_SCOPE = "guardrails:check"
_CLOCK_SKEW_SECONDS = 60


@dataclass
class Principal:
    """Resolved caller identity for a request."""

    tenant_id: str
    agent_id: str | None
    scopes: list[str]
    principal_type: str  # 'agent' | 'api_key' | 'service' | 'on_behalf_of_user'
    api_key_id: str | None = None
    raw_claims: dict[str, Any] = field(default_factory=dict)


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
            # Accept the platform audience OR the service-token wildcard "*" (Contract 12 mints
            # internal service JWTs with aud="*"). PyJWT does literal audience matching, so the
            # wildcard must be listed explicitly — a bare agent JWT (aud=auth_platform_audience)
            # and a service JWT (aud="*") both verify here.
            audience=[settings.auth_platform_audience, "*"],
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


# ── Revocation mirror (WP03 — shared kill-switch; FAIL-OPEN on Valkey trouble) ──
# Verifier-side mirror of the Auth-owned Valkey revocation scheme. AFTER signature /
# iss / aud / exp / scope pass, a token is rejected (401 TOKEN_REVOKED) if ANY of:
#   * <prefix>jti:{jti}        exists      — that specific token was revoked
#   * <prefix>kid:{kid}        exists      — its signing key was poisoned / emergency-rotated
#   * <prefix>agent:{agent_id} exists AND token.iat < that unix-epoch — "revoke all for agent"
# Revocation is defense-in-depth: if Valkey is unavailable the check FAILS OPEN (accept +
# log revocation_check_skipped=true + metric) so a cache outage never takes auth down.


def _kid_of(token: str) -> str | None:
    """Best-effort signing-key id from the JWT header (None if unparseable)."""
    try:
        return jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None


async def _assert_not_revoked(
    *,
    token: str,
    claims: dict[str, Any],
    valkey: ValkeyClient | None,
    settings: Settings,
) -> None:
    """Reject (401 TOKEN_REVOKED) on a revocation hit; FAIL OPEN on Valkey trouble.

    Checks the three shared keys for this token's jti / kid / agent_id (+ iat). A genuine
    cache miss (key absent) is a clean pass; only a Valkey error/timeout fails open.
    """
    if not settings.revocation_check_enabled:
        return
    if valkey is None:
        # No client wired (e.g. a bare test app) — same posture as an outage: fail open.
        metrics.revocation_checks_total.labels(outcome="skipped").inc()
        logger.warning("revocation_check_skipped", reason="no_valkey_client")
        return

    prefix = settings.revocation_key_prefix
    budget = settings.revocation_valkey_timeout_seconds
    jti = claims.get("jti")
    kid = _kid_of(token)
    agent_id = claims.get("agent_id")
    iat = claims.get("iat")

    async def _get(key: str) -> str | None:
        return await valkey.get(key, timeout_seconds=budget)

    reason: str | None
    try:
        if jti and await _get(f"{prefix}jti:{jti}") is not None:
            reason = "jti"
        elif kid and await _get(f"{prefix}kid:{kid}") is not None:
            reason = "kid"
        elif agent_id and isinstance(iat, int | float):
            epoch = await _get(f"{prefix}agent:{agent_id}")
            reason = "agent" if epoch is not None and int(iat) < int(epoch) else None
        else:
            reason = None
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: defense-in-depth fails OPEN
        metrics.revocation_checks_total.labels(outcome="skipped").inc()
        logger.warning("revocation_check_skipped", reason="valkey_error", error=str(exc))
        return

    if reason is not None:
        metrics.revocation_checks_total.labels(outcome="revoked").inc()
        logger.info("token_revoked", reason=reason, agent_id=str(agent_id) if agent_id else None)
        raise ApiError(ErrorCode.TOKEN_REVOKED, "Token has been revoked.")
    metrics.revocation_checks_total.labels(outcome="clean").inc()


def _resolve_principal(request: Request, settings: Settings) -> tuple[Principal, list[_Verified]]:
    """Pure resolution logic; the FastAPI dependency is a thin async wrapper.

    Returns the Principal AND the list of verified tokens (bearer, and the forwarded
    agent JWT in INTERNAL mode) so the async wrapper can run the revocation mirror on
    every credential — a revoked agent must not slip through service-token forwarding.
    """
    bearer_token = _bearer(request)
    bearer_claims = _decode(bearer_token, settings)
    sub = str(bearer_claims.get("sub", ""))

    forwarded = request.headers.get("x-forwarded-agent-jwt")
    verified: list[_Verified] = [_Verified(bearer_token, bearer_claims)]

    if sub.startswith("svc:") or sub.startswith("svc-ext:"):
        # ── INTERNAL mode: service token + forwarded agent JWT ───────────────
        if not forwarded:
            raise ApiError(
                ErrorCode.UNAUTHORIZED,
                "Service token requires X-Forwarded-Agent-JWT header.",
            )
        agent_claims = _decode(forwarded, settings)
        verified.append(_Verified(forwarded, agent_claims))
        agent_id = agent_claims.get("agent_id")
        on_behalf_of = bearer_claims.get("on_behalf_of")
        if not on_behalf_of or on_behalf_of != agent_id:
            raise ApiError(
                ErrorCode.UNAUTHORIZED,
                "Service token on_behalf_of does not match forwarded agent JWT agent_id.",
            )
        principal = _principal_from_agent_claims(agent_claims, principal_type="service")
    elif forwarded:
        # A forwarded agent JWT only makes sense alongside a service token.
        raise ApiError(
            ErrorCode.UNAUTHORIZED,
            "X-Forwarded-Agent-JWT is only valid with a service-token Authorization.",
        )
    else:
        # ── EXTERNAL mode: bare agent / api-key JWT ───────────────────────────
        ptype = "api_key" if bearer_claims.get("api_key_id") else "agent"
        principal = _principal_from_agent_claims(bearer_claims, principal_type=ptype)

    if REQUIRED_SCOPE not in principal.scopes:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            f"Token missing required scope '{REQUIRED_SCOPE}'.",
        )
    return principal, verified


@dataclass
class _Verified:
    """A signature/iss/aud/exp-verified token + its decoded claims (revocation input)."""

    token: str
    claims: dict[str, Any]


def _principal_from_agent_claims(claims: dict[str, Any], *, principal_type: str) -> Principal:
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Agent token missing tenant_id claim.")
    return Principal(
        tenant_id=str(tenant_id),
        agent_id=str(claims["agent_id"]) if claims.get("agent_id") else None,
        scopes=_scopes_of(claims),
        principal_type=principal_type,
        api_key_id=str(claims["api_key_id"]) if claims.get("api_key_id") else None,
        raw_claims=claims,
    )


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: verify auth, run the revocation mirror, return the Principal."""
    settings = get_settings()
    principal, verified = _resolve_principal(request, settings)
    # Mirror the shared kill-switch on EVERY verified credential. In INTERNAL mode this
    # covers BOTH the service token (its jti/kid) AND the forwarded agent JWT
    # (jti/kid/agent-epoch) so a revoked agent cannot slip through service forwarding.
    valkey: ValkeyClient | None = getattr(request.app.state, "valkey", None)
    for v in verified:
        await _assert_not_revoked(
            token=v.token, claims=v.claims, valkey=valkey, settings=settings
        )
    return principal
