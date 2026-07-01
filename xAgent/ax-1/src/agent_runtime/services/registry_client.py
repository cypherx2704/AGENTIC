"""Tool-Registry client (tool-loop / skill-load stages, WP12).

Resolves a tool to its manifest + invoke URL + required scopes via the Tool Registry:

  * ``GET /v1/tools``                       -> list available tools (no cache)
  * ``GET /v1/tools/{name}?version=<v>``    -> resolve one tool (ETag-cached, see below)

Identity flows via HEADERS only (Contract 13) — no identity in the query/body:

  * ``Authorization: Bearer <xAgent service JWT>``     (Contract 12, on_behalf_of=agent)
  * ``X-Forwarded-Agent-JWT: <inbound agent JWT>``      (verbatim forward, Phase 9 rule)
  * ``traceparent`` + ``tracestate`` + ``X-Request-ID`` (Contract 8 W3C propagation)

── 5-minute ETag manifest cache (in-process) ─────────────────────────────────────────────
``resolve_tool`` caches each resolved tool IN-PROCESS, keyed by ``"{name}@{version|'latest'}"``,
storing the response body, its ``ETag``, and a monotonic ``fresh_until`` deadline
(now + ``registry_manifest_cache_ttl_seconds``, default 300s):

  * FRESH (within TTL)         -> served straight from cache, NO network call (hit).
  * STALE (TTL expired)        -> re-validate with ``If-None-Match: <etag>``:
        - 304 Not Modified  -> reuse the cached body and RESET the TTL (revalidated).
        - 200 OK            -> replace the body + ETag, reset the TTL (refreshed).
  * MISS (never cached)        -> full GET, then backfill (miss).

FAIL-SOFT: a transport error / 5xx during a re-validation serves the EXISTING cached entry
stale (a registry blip must not break a tool the agent already knows) — logged + counted.
With no cached entry to fall back on, a transport/5xx error is retried up to
``registry_retry_attempts`` times and then raised as SERVICE_UNAVAILABLE; a 404 is NOT_FOUND;
any other 4xx is terminal SERVICE_UNAVAILABLE (never retried). The cache is per-process
(per worker) — acceptable because manifests are immutable per (name, version) and the 5-min
TTL bounds staleness for ``latest``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from ..core import metrics, trace
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from .service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@dataclass
class ToolResolution:
    """A resolved tool — manifest + how/where to invoke it + the scopes it needs."""

    name: str
    version: str
    manifest: dict[str, Any]
    invoke_url: str
    required_scopes: list[str] = field(default_factory=list)


@dataclass
class _CachedManifest:
    resolution: ToolResolution
    etag: str | None
    fresh_until: float  # monotonic seconds — re-validate once now() passes this


class RegistryClient:
    """Thin async client for the Tool-Registry resolve + list endpoints (ETag-cached)."""

    def __init__(
        self,
        settings: Settings,
        token_provider: ServiceTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = client  # injectable for tests (respx); lazily created otherwise
        self._owns_client = client is None
        # In-process manifest cache keyed by "{name}@{version|'latest'}".
        self._cache: dict[str, _CachedManifest] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.registry_timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _headers(
        self, *, agent_jwt: str, on_behalf_of: str | None, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
        headers = {
            "Authorization": f"Bearer {service_jwt}",
            "X-Forwarded-Agent-JWT": agent_jwt,
            **trace.propagation_headers(),
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _cache_key(name: str, version: str | None) -> str:
        return f"{name}@{version or 'latest'}"

    async def list_tools(
        self, *, agent_jwt: str, on_behalf_of: str | None = None
    ) -> list[ToolResolution]:
        """List all tools the registry exposes (uncached — the listing changes often)."""
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        url = f"{self._settings.tool_registry_url.rstrip('/')}/v1/tools"
        try:
            resp = await self._http().get(url, headers=headers)
        except httpx.HTTPError as exc:
            metrics.downstream_calls_total.labels("registry", "error").inc()
            logger.warning("registry_list_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Tool registry unavailable.") from exc
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("registry", "rejected").inc()
            logger.warning("registry_list_rejected", status=resp.status_code)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Tool registry returned {resp.status_code}.",
            )
        metrics.downstream_calls_total.labels("registry", "ok").inc()
        data = resp.json()
        items = data.get("tools", data) if isinstance(data, dict) else data
        return [self._parse(item) for item in (items or [])]

    async def resolve_tool(
        self,
        name: str,
        version: str | None = None,
        *,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> ToolResolution:
        """Resolve ``name`` (optionally pinned to ``version``) to a :class:`ToolResolution`.

        Served from the 5-min ETag cache when fresh; re-validated with ``If-None-Match``
        when stale (304 -> reuse + reset TTL, 200 -> refresh). On a registry blip a still
        -cached entry is served stale. Raises NOT_FOUND on a 404 (no fallback), and
        SERVICE_UNAVAILABLE on a transport/5xx with no cache to fall back on, or any
        other terminal 4xx.
        """
        key = self._cache_key(name, version)
        cached = self._cache.get(key)
        now = time.monotonic()

        if cached is not None and cached.fresh_until > now:
            metrics.registry_manifest_cache_total.labels("hit").inc()
            return cached.resolution

        extra = {"If-None-Match": cached.etag} if (cached and cached.etag) else None
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of, extra=extra)

        params = {"version": version} if version else None
        url = f"{self._settings.tool_registry_url.rstrip('/')}/v1/tools/{name}"

        resp = await self._get_with_retry(url, headers, params, cached, name)

        # Re-validation hit: reuse the cached body, reset the TTL.
        if resp.status_code == 304 and cached is not None:
            metrics.registry_manifest_cache_total.labels("revalidated").inc()
            metrics.downstream_calls_total.labels("registry", "ok").inc()
            cached.fresh_until = now + self._settings.registry_manifest_cache_ttl_seconds
            return cached.resolution

        if resp.status_code == 404:
            metrics.downstream_calls_total.labels("registry", "rejected").inc()
            logger.warning("registry_resolve_not_found", name=name, version=version)
            raise ApiError(ErrorCode.NOT_FOUND, f"Tool {name!r} not found in registry.")
        if resp.status_code >= 400:
            metrics.downstream_calls_total.labels("registry", "rejected").inc()
            logger.warning("registry_resolve_rejected", name=name, status=resp.status_code)
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                f"Tool registry returned {resp.status_code}.",
            )

        # 200 OK — fresh body. Backfill / refresh the cache entry + its ETag.
        metrics.registry_manifest_cache_total.labels("refreshed" if cached else "miss").inc()
        metrics.downstream_calls_total.labels("registry", "ok").inc()
        resolution = self._parse(resp.json())
        self._cache[key] = _CachedManifest(
            resolution=resolution,
            etag=resp.headers.get("ETag"),
            fresh_until=now + self._settings.registry_manifest_cache_ttl_seconds,
        )
        return resolution

    async def _get_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        cached: _CachedManifest | None,
        name: str,
    ) -> httpx.Response:
        """GET with retry on transport/5xx; serve a stale cache entry when one exists.

        A 4xx response is returned to the caller untouched (terminal — never retried).
        With a cached entry present, a transport/5xx error is swallowed and surfaced as a
        synthetic 304 so the caller serves the cached body stale (fail-soft). Without a
        cache to fall back on, the error is retried up to ``registry_retry_attempts`` and
        then raised SERVICE_UNAVAILABLE.
        """
        attempts = max(1, self._settings.registry_retry_attempts + 1)
        last_exc: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                resp = await self._http().get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("registry_resolve_attempt_failed", name=name, attempt=attempt, error=str(exc))
                continue
            # A 5xx is retryable; a 4xx (incl. 304/404) is terminal -> return as-is.
            if resp.status_code >= 500 and attempt < attempts - 1:
                logger.warning(
                    "registry_resolve_5xx_retry", name=name, attempt=attempt, status=resp.status_code
                )
                continue
            if resp.status_code >= 500 and cached is not None:
                # Exhausted retries on a 5xx but we have a cached copy — serve it stale.
                metrics.registry_manifest_cache_total.labels("stale").inc()
                logger.warning("registry_resolve_served_stale", name=name, status=resp.status_code)
                return httpx.Response(304)
            return resp

        # All attempts raised a transport error.
        if cached is not None:
            metrics.registry_manifest_cache_total.labels("stale").inc()
            logger.warning("registry_resolve_served_stale_after_error", name=name)
            return httpx.Response(304)
        metrics.downstream_calls_total.labels("registry", "error").inc()
        raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Tool registry unavailable.") from last_exc

    async def get_tool_access(
        self,
        name: str,
        *,
        capability: str | None = None,
        agent_jwt: str,
        on_behalf_of: str | None = None,
    ) -> str:
        """Resolve the calling agent's effective access mode for a tool server.

        Returns one of ``none`` | ``ask`` | ``automated``. FAIL-CLOSED: any registry error or
        non-200 returns ``none`` — a tool whose access can't be confirmed is NOT invoked. (Access
        control is a security gate, unlike resolve_tool's fail-soft cache.)
        """
        headers = await self._headers(agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)
        params = {"capability": capability} if capability else None
        url = f"{self._settings.tool_registry_url.rstrip('/')}/v1/tools/{name}/access"
        try:
            resp = await self._http().get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.warning("tool_access_lookup_failed", name=name, error=str(exc))
            return "none"
        if resp.status_code != 200:
            logger.warning("tool_access_lookup_rejected", name=name, status=resp.status_code)
            return "none"
        try:
            mode = str(resp.json().get("access_mode", "none"))
        except Exception:  # noqa: BLE001 — malformed body → deny
            return "none"
        return mode if mode in ("none", "ask", "automated") else "none"

    @staticmethod
    def _parse(data: dict[str, Any]) -> ToolResolution:
        return ToolResolution(
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            manifest=data.get("manifest", {}) or {},
            invoke_url=str(data.get("invoke_url", "")),
            required_scopes=list(data.get("required_scopes", []) or []),
        )
