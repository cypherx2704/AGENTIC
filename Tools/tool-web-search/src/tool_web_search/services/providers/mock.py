"""Deterministic, network-free mock search provider.

The default provider for local dev and tests (``SEARCH_PROVIDER=mock``). Given a query
it synthesises ``max_results`` stable, query-derived results — same input always yields
the same output (so contract tests can assert exact bodies and idempotency replay can be
verified). NO network, NO API key.

A tiny escape hatch for the output-cap test: a query of the form ``__bloat__:<n>``
returns a single result whose snippet is ``<n>`` bytes of filler, letting a test push a
single invoke result past the 10 MiB cap deterministically without any real provider.
"""

from __future__ import annotations

from .base import SearchProvider, SearchResult

_BLOAT_PREFIX = "__bloat__:"


class MockSearchProvider(SearchProvider):
    """Stateless, deterministic canned-results provider."""

    name = "mock"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        if query.startswith(_BLOAT_PREFIX):
            return [self._bloat_result(query)]

        results: list[SearchResult] = []
        for i in range(max_results):
            rank = i + 1
            results.append(
                SearchResult(
                    title=f"Result {rank} for {query}",
                    url=f"https://example.com/search?q={query}&r={rank}",
                    snippet=(
                        f"This is a deterministic mock snippet #{rank} describing "
                        f"results for the query {query!r}."
                    ),
                    rank=rank,
                )
            )
        return results

    @staticmethod
    def _bloat_result(query: str) -> SearchResult:
        """Return one result whose snippet is N bytes of filler (output-cap test seam)."""
        try:
            n = int(query[len(_BLOAT_PREFIX) :])
        except ValueError:
            n = 0
        return SearchResult(
            title="bloat",
            url="https://example.com/bloat",
            snippet="x" * max(n, 0),
            rank=1,
        )
