"""Provider adaptor interface (Component 3).

Every provider implements :class:`ProviderAdaptor`: a non-streaming ``chat`` and a
streaming ``chat_stream`` that yields raw SSE chunk strings (already
``data: ...\\n\\n`` framed, terminating with ``data: [DONE]\\n\\n``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ...core.errors import ApiError, ErrorCode
from ...models.unified import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ClassifyRequest,
    ClassifyResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    RerankRequest,
    RerankResponse,
)


class ProviderAdaptor(ABC):
    """Abstract provider adaptor."""

    #: Provider key as stored in pricing/alias tables, e.g. "anthropic" | "openai".
    provider: str

    @abstractmethod
    async def chat(self, req: ChatCompletionRequest, *, model_id: str) -> ChatCompletionResponse:
        """Non-streaming completion. Returns a unified response (cost_usd unset)."""
        raise NotImplementedError

    @abstractmethod
    def chat_stream(self, req: ChatCompletionRequest, *, model_id: str) -> AsyncIterator[str]:
        """Streaming completion. Yields SSE-framed ``data:`` lines incl. final usage + [DONE]."""
        raise NotImplementedError

    async def embed(self, req: EmbeddingRequest, *, model_id: str) -> EmbeddingResponse:
        """Embed ``req.input``. Returns a unified response (cost_usd unset).

        NOT abstract: most providers don't expose embeddings (e.g. Anthropic), so the
        default raises a clear Contract-2 503 rather than forcing every adaptor to
        implement it. OpenAI + the mock provider override this.
        """
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"The '{self.provider}' provider does not support embeddings.",
            status_code=422,
            details={"model": model_id, "provider": self.provider},
        )

    async def rerank(self, req: RerankRequest, *, model_id: str) -> RerankResponse:
        """Rerank ``req.documents`` against ``req.query``. Returns a unified response.

        NOT abstract: most chat/embeddings providers don't rerank. The default raises a
        clear Contract-2 422 rather than forcing every adaptor to implement it. The
        dedicated rerank providers (mock + local cross-encoder seam) override this.
        """
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"The '{self.provider}' provider does not support reranking.",
            status_code=422,
            details={"model": model_id, "provider": self.provider},
        )

    async def classify(self, req: ClassifyRequest, *, model_id: str) -> ClassifyResponse:
        """Classify ``req.input`` for safety. Returns a unified response.

        NOT abstract: the default raises a clear Contract-2 422. The dedicated safety
        providers (stub + local safety-model seam) override this.
        """
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"The '{self.provider}' provider does not support classification.",
            status_code=422,
            details={"model": model_id, "provider": self.provider},
        )


class NonChatProvider(ProviderAdaptor):
    """Base for adaptors that serve ONLY a non-chat surface (rerank / safety classify).

    The chat surface is a hard requirement of :class:`ProviderAdaptor` (``chat`` /
    ``chat_stream`` are abstract). The rerank + safety providers don't do chat, so this
    intermediate implements those two to raise the standard Contract-2 422 — letting a
    rerank/classify-only adaptor override just ``rerank`` / ``classify``.
    """

    async def chat(self, req: ChatCompletionRequest, *, model_id: str) -> ChatCompletionResponse:
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"The '{self.provider}' provider does not support chat completions.",
            status_code=422,
            details={"model": model_id, "provider": self.provider},
        )

    async def chat_stream(
        self, req: ChatCompletionRequest, *, model_id: str
    ) -> AsyncIterator[str]:
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            f"The '{self.provider}' provider does not support chat completions.",
            status_code=422,
            details={"model": model_id, "provider": self.provider},
        )
        yield ""  # pragma: no cover — makes this an async generator (never reached)
