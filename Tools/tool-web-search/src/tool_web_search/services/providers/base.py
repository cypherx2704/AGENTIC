"""Search-provider interface + the result shape returned to the invoke handler.

A provider is anything implementing :class:`SearchProvider`: an async ``search(query,
max_results)`` returning a list of :class:`SearchResult`. The concrete provider is
selected by ``SEARCH_PROVIDER`` (``mock`` | ``serpapi`` | ``brave``) via
:func:`tool_web_search.services.providers.get_provider`. The ``mock`` provider needs no
network and is the default for local dev and tests; ``serpapi`` / ``brave`` make real
httpx calls (tests drive them via respx, never the live network).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """Raised by a real provider when the upstream call fails (-> 502/SERVICE_UNAVAILABLE)."""


@dataclass(frozen=True)
class SearchResult:
    """One ranked web-search result (matches the manifest's output_schema items)."""

    title: str
    url: str
    snippet: str
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class SearchProvider(Protocol):
    """The pluggable provider contract."""

    name: str

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return up to ``max_results`` ranked results for ``query``."""
        ...
