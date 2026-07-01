"""Contract 12 — service-token acquisition + caching.

xAgent authenticates to downstream services (Guardrails, LLMs) as the ``xagent``
service principal. It mints a short-lived (5-minute) SERVICE JWT from the Auth service
via ``POST /v1/service-tokens`` using the bootstrap secret, and caches it in-process
until shortly before expiry.

Auth-call shape (Phase 9 + Contract 12):
  * ``X-Service-Name: xagent``
  * ``X-Service-Bootstrap-Secret: <SERVICE_BOOTSTRAP_SECRET>``
  * body ``{ "on_behalf_of": "<agent_id>" }`` — the agent this call serves; downstream
    verifies it matches the forwarded agent JWT's ``agent_id`` (Contract 12). Because
    ``on_behalf_of`` varies per agent, the cache is keyed by ``on_behalf_of``.

The token string is returned for use as ``Authorization: Bearer <service-jwt>`` on the
downstream call. The inbound agent JWT is forwarded separately as
``X-Forwarded-Agent-JWT`` (the downstream clients add that header) — NOT via this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode

logger = structlog.get_logger(__name__)

# Refresh this many seconds BEFORE the nominal 5-minute expiry to avoid edge races.
_REFRESH_SKEW_SECONDS = 30
_DEFAULT_TTL_SECONDS = 300


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # monotonic seconds


class ServiceTokenProvider:
    """Acquires + caches xAgent service JWTs (one cache entry per ``on_behalf_of``)."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client  # injectable for tests (respx); lazily created otherwise
        self._owns_client = client is None
        self._cache: dict[str, _CachedToken] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def get_token(self, *, on_behalf_of: str | None = None) -> str:
        """Return a valid cached service JWT for ``on_behalf_of`` (minting if needed)."""
        key = on_behalf_of or ""
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and cached.expires_at - _REFRESH_SKEW_SECONDS > now:
            return cached.token
        token, ttl = await self._mint(on_behalf_of)
        self._cache[key] = _CachedToken(token=token, expires_at=now + ttl)
        return token

    async def _mint(self, on_behalf_of: str | None) -> tuple[str, float]:
        url = f"{self._settings.auth_service_url.rstrip('/')}/v1/service-tokens"
        headers = {
            "X-Service-Name": self._settings.service_principal_name,
            "X-Service-Bootstrap-Secret": self._settings.service_bootstrap_secret,
        }
        body: dict[str, str] = {}
        if on_behalf_of:
            body["on_behalf_of"] = on_behalf_of
        try:
            resp = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            logger.warning("service_token_mint_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Unable to mint xAgent service token.") from exc
        if resp.status_code >= 400:
            logger.warning("service_token_mint_rejected", status=resp.status_code)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Auth rejected service-token request ({resp.status_code}).",
            )
        data = resp.json()
        token = data.get("access_token") or data.get("token")
        if not token:
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Auth returned no service token.")
        ttl = float(data.get("expires_in", _DEFAULT_TTL_SECONDS))
        return token, ttl
