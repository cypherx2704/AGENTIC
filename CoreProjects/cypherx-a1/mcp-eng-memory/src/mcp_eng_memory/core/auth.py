"""Inbound JWT verification (Contracts 1, 12, 13) — dual-mode + dual scope.

EXTERNAL mode: the bearer is a bare agent / api-key-exchanged JWT (e.g. an external coding
agent that exchanged an API key). INTERNAL mode: the bearer is a Contract-12 SERVICE token
(``sub=svc:*``) plus ``X-Forwarded-Agent-JWT`` carrying the originating agent's JWT, where
the service token's ``on_behalf_of`` MUST equal the forwarded agent's ``agent_id``.

Both modes resolve to one :class:`Principal`; ``agent_jwt`` is the agent token this facade
FORWARDS to the cypherx-a1 backend, which independently re-verifies it (signature, claims,
AND the shared live-revocation mirror) and enforces tenant RLS. The facade itself stays
stateless (no Valkey) — revocation is enforced at the backend, by design.

Scopes: the coarse ``tool:invoke`` is required here (``require_principal``); the fine
``tool:mcp-eng-memory:invoke`` is checked in the invoke handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import jwt
import structlog
from fastapi import Request
from jwt import PyJWKClient

from .config import Settings, get_settings
from .errors import ApiError, ErrorCode

logger = structlog.get_logger(__name__)

_CLOCK_SKEW_SECONDS = 60


@dataclass
class Principal:
    tenant_id: str
    agent_id: str | None
    scopes: list[str]
    agent_jwt: str  # the agent token forwarded to the cypherx-a1 backend
    raw_claims: dict[str, Any] = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


_jwks_clients: dict[str, PyJWKClient] = {}


def get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        _jwks_clients[jwks_url] = client
    return client


def warm_jwks(settings: Settings) -> None:
    try:
        get_jwks_client(settings.auth_jwks_url).get_signing_keys()
        logger.info("jwks_warmed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("jwks_warm_failed", error=str(exc))


def _decode(token: str, settings: Settings) -> dict[str, Any]:
    client = get_jwks_client(settings.auth_jwks_url)
    try:
        key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token, key.key, algorithms=["RS256"],
            audience=settings.auth_platform_audience, issuer=settings.auth_issuer_url,
            leeway=_CLOCK_SKEW_SECONDS, options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ApiError(ErrorCode.UNAUTHORIZED, f"Invalid token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — JWKS fetch failures
        raise ApiError(ErrorCode.UNAUTHORIZED, "Unable to verify token signing key.") from exc


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Missing or malformed Authorization header.")
    return token.strip()


def _scopes_of(claims: dict[str, Any]) -> list[str]:
    scopes = claims.get("scopes", [])
    if isinstance(scopes, str):
        return scopes.split()
    return [str(s) for s in scopes] if isinstance(scopes, list) else []


async def require_principal(request: Request) -> Principal:
    settings = get_settings()
    bearer = _bearer(request)
    claims = _decode(bearer, settings)
    forwarded = request.headers.get("x-forwarded-agent-jwt")

    if forwarded:
        # INTERNAL service-token mode.
        sub = str(claims.get("sub", ""))
        if not (sub.startswith("svc:") or sub.startswith("svc-ext:")):
            raise ApiError(ErrorCode.UNAUTHORIZED, "X-Forwarded-Agent-JWT requires a service-token bearer.")
        fwd_claims = _decode(forwarded, settings)
        # Contract-12: in internal mode the service token's on_behalf_of MUST be present AND
        # equal the forwarded agent's agent_id. Enforce unconditionally — a missing claim or
        # a missing forwarded agent_id is an authorization failure, not a silent pass.
        obo = claims.get("on_behalf_of")
        fwd_agent_id = fwd_claims.get("agent_id")
        if not obo or not fwd_agent_id or str(obo) != str(fwd_agent_id):
            raise ApiError(
                ErrorCode.UNAUTHORIZED,
                "Service token on_behalf_of must be present and equal the forwarded agent_id.",
            )
        tenant_id = fwd_claims.get("tenant_id") or claims.get("tenant_id")
        scopes = _scopes_of(claims)  # tool scopes are on the calling service token
        principal = Principal(
            tenant_id=str(tenant_id) if tenant_id else "",
            agent_id=str(fwd_claims["agent_id"]) if fwd_claims.get("agent_id") else None,
            scopes=scopes, agent_jwt=forwarded, raw_claims=fwd_claims,
        )
    else:
        # EXTERNAL bare-JWT mode.
        tenant_id = claims.get("tenant_id")
        principal = Principal(
            tenant_id=str(tenant_id) if tenant_id else "",
            agent_id=str(claims["agent_id"]) if claims.get("agent_id") else None,
            scopes=_scopes_of(claims), agent_jwt=bearer, raw_claims=claims,
        )

    if not principal.tenant_id:
        raise ApiError(ErrorCode.UNAUTHORIZED, "Token missing tenant_id claim.")
    if not principal.has_scope(settings.coarse_scope):
        raise ApiError(ErrorCode.FORBIDDEN, f"Token missing required scope '{settings.coarse_scope}'.")
    return principal
