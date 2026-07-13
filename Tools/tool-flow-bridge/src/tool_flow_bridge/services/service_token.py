"""Contract 12 — service-token acquisition + caching.

The bridge authenticates to the Tool Registry as the ``tool-flow-bridge`` service principal
using INTERNAL mode: it mints a short-lived (~5-minute) SERVICE JWT from Auth via
``POST /v1/service-tokens`` (bootstrap secret) with ``on_behalf_of=<user agent_id>``, and
forwards the user's own agent JWT as ``X-Forwarded-Agent-JWT``. The registry then takes the
tenant_id + ``tool:admin`` scope from the FORWARDED user JWT (never the service token).

Cached in-process, keyed by ``on_behalf_of``. Verbatim shape of the cypherx-a1 provider.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode

logger = structlog.get_logger(__name__)

_REFRESH_SKEW_SECONDS = 30
_DEFAULT_TTL_SECONDS = 300


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # monotonic seconds


class ServiceTokenProvider:
    """Acquires + caches tool-flow-bridge service JWTs (one entry per ``on_behalf_of``)."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
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
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, "Unable to mint bridge service token."
            ) from exc
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
