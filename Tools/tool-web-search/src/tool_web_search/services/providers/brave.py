"""Brave-Search-backed search provider (``SEARCH_PROVIDER=brave``).

Real httpx GET against ``brave_base_url`` (Brave Search API) with the configured
``BRAVE_API_KEY`` in the ``X-Subscription-Token`` header. Maps ``web.results`` to the
canonical :class:`SearchResult` shape. Tests exercise this via respx — never the live
network in CI.
"""

from __future__ import annotations

import httpx
import structlog

from .base import ProviderError, SearchProvider, SearchResult

logger = structlog.get_logger(__name__)


class BraveSearchProvider(SearchProvider):
    """Brave Search API adapter."""

    name = "brave"

    def __init__(self, api_key: str | None, base_url: str, *, timeout_seconds: float) -> None:
        if not api_key:
            raise ProviderError("BRAVE_API_KEY is required for the brave provider.")
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout_seconds

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = {"q": query, "count": max_results}
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self._base_url, params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("brave_call_failed", error=str(exc))
            raise ProviderError(f"Brave request failed: {exc}") from exc

        web_results = (payload.get("web") or {}).get("results") or []
        results: list[SearchResult] = []
        for i, item in enumerate(web_results[:max_results]):
            results.append(
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    snippet=str(item.get("description", "")),
                    rank=i + 1,
                )
            )
        return results
