"""Embedding client — deterministic mock, gateway path, and fail-open fallback."""

from __future__ import annotations

import httpx
import pytest
import respx

from memory_service.core.config import Settings
from memory_service.services import embeddings
from memory_service.services.embeddings import EmbeddingClient, pseudo_vector


def test_pseudo_vector_is_deterministic_and_normalized() -> None:
    v1 = pseudo_vector("hello world", 1536)
    v2 = pseudo_vector("hello world", 1536)
    assert v1 == v2  # deterministic
    assert len(v1) == 1536
    # L2-normalized (within rounding).
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-3


def test_pseudo_vector_differs_for_different_text() -> None:
    assert pseudo_vector("apples", 64) != pseudo_vector("oranges", 64)


@pytest.mark.asyncio
async def test_mock_mode_never_calls_network() -> None:
    settings = Settings(embeddings_mock_fallback=True, embeddings_vector_dim=64)
    client = EmbeddingClient(settings)
    vecs, source = await client.embed_many(["a", "b"])
    assert source == "mock"
    assert len(vecs) == 2 and all(len(v) == 64 for v in vecs)


@pytest.mark.asyncio
@respx.mock
async def test_gateway_path_used_when_not_mock() -> None:
    settings = Settings(
        embeddings_mock_fallback=False, mock_providers=False,
        embeddings_base_url="http://gw.test", embeddings_vector_dim=4,
    )
    route = respx.post("http://gw.test/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "model": "embed",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
                    {"object": "embedding", "index": 1, "embedding": [0.5, 0.6, 0.7, 0.8]},
                ],
                "usage": {"prompt_tokens": 2, "total_tokens": 2, "cost_usd": 0.0},
            },
        )
    )
    client = EmbeddingClient(settings)
    vecs, source = await client.embed_many(["x", "y"])
    assert route.called
    assert source == "gateway"
    assert vecs[0] == [0.1, 0.2, 0.3, 0.4]
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_gateway_failure_falls_open_to_mock() -> None:
    settings = Settings(
        embeddings_mock_fallback=False, mock_providers=False,
        embeddings_base_url="http://gw.test", embeddings_vector_dim=8,
    )
    respx.post("http://gw.test/v1/embeddings").mock(return_value=httpx.Response(503))
    client = EmbeddingClient(settings)
    vecs, source = await client.embed_many(["x"])
    # Gateway 503 -> FALL OPEN to the deterministic mock; never raises.
    assert source == "mock"
    assert vecs == [pseudo_vector("x", 8)]
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_gateway_index_order_normalized() -> None:
    settings = Settings(
        embeddings_mock_fallback=False, mock_providers=False,
        embeddings_base_url="http://gw.test", embeddings_vector_dim=2,
    )
    # Gateway returns data OUT of index order; the client must re-sort by index.
    respx.post("http://gw.test/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list", "model": "embed",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [9.0, 9.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0, 1.0]},
                ],
                "usage": {"prompt_tokens": 2, "total_tokens": 2, "cost_usd": 0.0},
            },
        )
    )
    client = EmbeddingClient(settings)
    vecs, _ = await client.embed_many(["first", "second"])
    assert vecs[0] == [1.0, 1.0]  # index 0 first
    assert vecs[1] == [9.0, 9.0]
    await client.close()


def test_settings_either_flag_forces_mock() -> None:
    # Either flag forces the offline embedder; with both off, the gateway is used. (We
    # pass explicit overrides because the test process env pins both flags on for the app
    # suite — Settings() would otherwise inherit those.)
    assert Settings(embeddings_mock_fallback=True, mock_providers=False).use_mock_embeddings is True
    assert Settings(embeddings_mock_fallback=False, mock_providers=True).use_mock_embeddings is True
    assert Settings(embeddings_mock_fallback=False, mock_providers=False).use_mock_embeddings is False
    # Confirm the module exposes the pseudo embedder (the documented offline fallback).
    assert embeddings.pseudo_vector is pseudo_vector
