"""Embedding mock-fallback + determinism tests."""

from __future__ import annotations

import httpx
import pytest

from rag_service.core.config import Settings
from rag_service.services.embeddings import EmbeddingClient, mock_embed


def test_mock_vectors_are_deterministic_and_normalized() -> None:
    v1 = mock_embed(["hello"], 1536)[0]
    v2 = mock_embed(["hello"], 1536)[0]
    assert v1 == v2
    assert len(v1) == 1536
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-3


def test_mock_vectors_differ_per_text() -> None:
    assert mock_embed(["a"], 64)[0] != mock_embed(["b"], 64)[0]


@pytest.mark.asyncio
async def test_embed_uses_mock_when_mock_mode() -> None:
    settings = Settings(mock_embeddings=True)
    client = EmbeddingClient(settings)
    result = await client.embed(["one", "two"], dim=1536)
    assert result.source == "mock"
    assert len(result.vectors) == 2
    assert all(len(v) == 1536 for v in result.vectors)


@pytest.mark.asyncio
async def test_embed_falls_back_to_mock_on_llms_failure() -> None:
    # Real mode, but the llms call raises -> deterministic fallback (fallback_to_mock default).
    settings = Settings(mock_embeddings=False, embeddings_fallback_to_mock=True)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("llms down")

    transport = httpx.MockTransport(_boom)
    http = httpx.AsyncClient(transport=transport)

    class _FakeTokens:
        async def get_token(self, *, on_behalf_of=None):  # noqa: ANN001
            return "svc.jwt"

    client = EmbeddingClient(settings, token_provider=_FakeTokens(), client=http)
    result = await client.embed(["q"], dim=1536, agent_jwt="a.jwt")
    assert result.source == "fallback_mock"
    assert len(result.vectors[0]) == 1536


@pytest.mark.asyncio
async def test_embed_via_llms_parses_response() -> None:
    settings = Settings(mock_embeddings=False)

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1] * 4, "index": 1},
                    {"embedding": [0.2] * 4, "index": 0},
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 7},
            },
        )

    transport = httpx.MockTransport(_ok)
    http = httpx.AsyncClient(transport=transport)

    class _FakeTokens:
        async def get_token(self, *, on_behalf_of=None):  # noqa: ANN001
            return "svc.jwt"

    client = EmbeddingClient(settings, token_provider=_FakeTokens(), client=http)
    result = await client.embed(["x", "y"], dim=4, agent_jwt="a.jwt")
    assert result.source == "llms"
    # Index-ordered: index 0 first.
    assert result.vectors[0] == [0.2] * 4
    assert result.vectors[1] == [0.1] * 4
    assert result.prompt_tokens == 7
