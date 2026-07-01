"""SerpApi-backed search provider (``SEARCH_PROVIDER=serpapi``).

Real httpx GET against ``serpapi_base_url`` (default https://serpapi.com/search.json)
with the configured ``SERPAPI_API_KEY``. Maps SerpApi's ``organic_results`` to the
canonical :class:`SearchResult` shape. Tests exercise this via respx (mocked transport)
— it is never hit on the live network in CI.
"""

from __future__ import annotations

import httpx
import structlog

from .base import ProviderError, SearchProvider, SearchResult

logger = structlog.get_logger(__name__)


class SerpApiSearchProvider(SearchProvider):
    """SerpApi Google-search adapter."""

    name = "serpapi"

    def __init__(self, api_key: str | None, base_url: str, *, timeout_seconds: float) -> None:
        if not api_key:
            raise ProviderError("SERPAPI_API_KEY is required for the serpapi provider.")
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout_seconds

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = {
            "q": query,
            "num": max_results,
            "engine": "google",
            "api_key": self._api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._base_url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("serpapi_call_failed", error=str(exc))
            raise ProviderError(f"SerpApi request failed: {exc}") from exc

        organic = payload.get("organic_results") or []
        results: list[SearchResult] = []
        for i, item in enumerate(organic[:max_results]):
            results.append(
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("link", "")),
                    snippet=str(item.get("snippet", "")),
                    rank=int(item.get("position", i + 1)),
                )
            )
        return results
