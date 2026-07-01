"""Search-provider adapters: mock determinism + serpapi/brave via respx (no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from tool_web_search.core.config import Settings
from tool_web_search.services.providers import ProviderError, get_provider
from tool_web_search.services.providers.brave import BraveSearchProvider
from tool_web_search.services.providers.mock import MockSearchProvider
from tool_web_search.services.providers.serpapi import SerpApiSearchProvider


@pytest.mark.asyncio
async def test_mock_is_deterministic_and_bounded() -> None:
    provider = MockSearchProvider()
    a = await provider.search("python", 3)
    b = await provider.search("python", 3)
    assert [r.to_dict() for r in a] == [r.to_dict() for r in b]
    assert len(a) == 3
    assert [r.rank for r in a] == [1, 2, 3]


def test_get_provider_selects_by_env() -> None:
    assert get_provider(Settings(search_provider="mock")).name == "mock"
    with pytest.raises(ProviderError):
        get_provider(Settings(search_provider="nope"))


def test_real_providers_require_keys() -> None:
    with pytest.raises(ProviderError):
        SerpApiSearchProvider(None, "https://serpapi.com/search.json", timeout_seconds=5)
    with pytest.raises(ProviderError):
        BraveSearchProvider(None, "https://api.search.brave.com", timeout_seconds=5)


@pytest.mark.asyncio
@respx.mock
async def test_serpapi_maps_organic_results() -> None:
    route = respx.get("https://serpapi.com/search.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "organic_results": [
                    {"title": "T1", "link": "https://a.example", "snippet": "s1", "position": 1},
                    {"title": "T2", "link": "https://b.example", "snippet": "s2", "position": 2},
                ]
            },
        )
    )
    provider = SerpApiSearchProvider("fake-key", "https://serpapi.com/search.json", timeout_seconds=5)
    results = await provider.search("query", 5)
    assert route.called
    assert [r.to_dict() for r in results] == [
        {"title": "T1", "url": "https://a.example", "snippet": "s1", "rank": 1},
        {"title": "T2", "url": "https://b.example", "snippet": "s2", "rank": 2},
    ]


@pytest.mark.asyncio
@respx.mock
async def test_brave_maps_web_results() -> None:
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "B1", "url": "https://x.example", "description": "d1"},
                    ]
                }
            },
        )
    )
    provider = BraveSearchProvider(
        "fake-key", "https://api.search.brave.com/res/v1/web/search", timeout_seconds=5
    )
    results = await provider.search("query", 5)
    assert results[0].to_dict() == {
        "title": "B1",
        "url": "https://x.example",
        "snippet": "d1",
        "rank": 1,
    }


@pytest.mark.asyncio
@respx.mock
async def test_serpapi_upstream_error_raises_provider_error() -> None:
    respx.get("https://serpapi.com/search.json").mock(return_value=httpx.Response(500))
    provider = SerpApiSearchProvider("fake-key", "https://serpapi.com/search.json", timeout_seconds=5)
    with pytest.raises(ProviderError):
        await provider.search("query", 5)
