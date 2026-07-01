"""Pluggable search-provider adapters (mock | serpapi | brave).

:func:`get_provider` resolves the concrete :class:`SearchProvider` from
``settings.search_provider``. The ``mock`` provider is the default and needs no network
or keys; ``serpapi`` / ``brave`` construct real httpx-backed adapters.
"""

from __future__ import annotations

from ...core.config import Settings
from .base import ProviderError, SearchProvider, SearchResult
from .brave import BraveSearchProvider
from .mock import MockSearchProvider
from .serpapi import SerpApiSearchProvider

__all__ = [
    "ProviderError",
    "SearchProvider",
    "SearchResult",
    "get_provider",
]


def get_provider(settings: Settings) -> SearchProvider:
    """Return the configured search provider; raises :class:`ProviderError` on a bad name."""
    name = settings.search_provider.strip().lower()
    if name == "mock":
        return MockSearchProvider()
    if name == "serpapi":
        return SerpApiSearchProvider(
            settings.serpapi_api_key,
            settings.serpapi_base_url,
            timeout_seconds=settings.provider_timeout_seconds,
        )
    if name == "brave":
        return BraveSearchProvider(
            settings.brave_api_key,
            settings.brave_base_url,
            timeout_seconds=settings.provider_timeout_seconds,
        )
    raise ProviderError(f"Unknown SEARCH_PROVIDER: {settings.search_provider!r}")
