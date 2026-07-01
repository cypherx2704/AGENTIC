"""Deterministic mock provider — no network, no keys.

Produces an echo-ish completion plus synthetic usage (token counts derived from
the input/output text) and a canned SSE stream that ends with a final usage chunk.
Cost is computed via :mod:`cost` so ``usage.cost_usd > 0`` for known models.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import AsyncIterator

from ...models.unified import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
    ResponseMessage,
    TextContent,
    Usage,
)
from ..cost import cost_calculator
from .base import ProviderAdaptor

# Mock model maps to anthropic pricing so cost > 0 regardless of resolved provider.
_MOCK_PROVIDER = "anthropic"
_MOCK_MODEL = "claude-haiku-4-5"

# Embeddings: price on the OpenAI text-embedding-3-small row so cost > 0 for the
# default `embed` alias path (output cost is 0 by convention for embeddings).
_MOCK_EMBED_PROVIDER = "openai"
_MOCK_EMBED_MODEL = "text-embedding-3-small"
# Native dimension returned when the request omits `dimensions` (matches the seeded
# text-embedding-3-small embedding_dim).
_MOCK_EMBED_DIM = 1536


def _estimate_tokens(text: str) -> int:
    # ~4 chars/token heuristic; minimum 1 so cost is always > 0.
    return max(1, len(text) // 4)


def _flatten(content: str | list | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.text for p in content if isinstance(p, TextContent))


def _build(req: ChatCompletionRequest, model_id: str) -> tuple[str, Usage]:
    last_user = next(
        (_flatten(m.content) for m in reversed(req.messages) if m.role == "user"),
        "",
    )
    reply = f"[mock:{model_id}] You said: {last_user}".strip()

    prompt_tokens = sum(_estimate_tokens(_flatten(m.content)) for m in req.messages) or 1
    completion_tokens = _estimate_tokens(reply)
    cost = cost_calculator.compute(
        _MOCK_PROVIDER,
        _MOCK_MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost,
    )
    return reply, usage


def _pseudo_vector(text: str, dim: int) -> list[float]:
    """Deterministic L2-normalized pseudo-embedding of ``dim`` floats for ``text``.

    Seeded from a SHA-256 digest of the text so the same input always yields the same
    vector (tests can assert determinism) with no network. Values land in [-1, 1] and
    the vector is unit-normalized so it looks like a real embedding.
    """
    raw: list[float] = []
    counter = 0
    while len(raw) < dim:
        digest = hashlib.sha256(f"{text}|{counter}".encode()).digest()
        for b in digest:
            raw.append((b / 127.5) - 1.0)  # byte 0..255 -> [-1, 1]
            if len(raw) >= dim:
                break
        counter += 1
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [round(v / norm, 6) for v in raw]


class MockProvider(ProviderAdaptor):
    provider = _MOCK_PROVIDER

    async def embed(self, req: EmbeddingRequest, *, model_id: str) -> EmbeddingResponse:
        texts = [req.input] if isinstance(req.input, str) else list(req.input)
        dim = req.dimensions or _MOCK_EMBED_DIM
        prompt_tokens = sum(_estimate_tokens(t) for t in texts) or 1
        cost = cost_calculator.compute(
            _MOCK_EMBED_PROVIDER,
            _MOCK_EMBED_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
        )
        return EmbeddingResponse(
            model=model_id,
            data=[
                EmbeddingData(embedding=_pseudo_vector(text, dim), index=i)
                for i, text in enumerate(texts)
            ],
            usage=EmbeddingUsage(
                prompt_tokens=prompt_tokens, total_tokens=prompt_tokens, cost_usd=cost
            ),
        )

    async def chat(self, req: ChatCompletionRequest, *, model_id: str) -> ChatCompletionResponse:
        reply, usage = _build(req, model_id)
        return ChatCompletionResponse(
            model=model_id,
            choices=[
                Choice(index=0, message=ResponseMessage(content=reply), finish_reason="stop")
            ],
            usage=usage,
        )

    async def chat_stream(
        self, req: ChatCompletionRequest, *, model_id: str
    ) -> AsyncIterator[str]:
        reply, usage = _build(req, model_id)
        resp_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        def chunk(delta: dict, finish: str | None = None, usage_obj: dict | None = None) -> str:
            payload: dict = {
                "id": resp_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage_obj is not None:
                payload["usage"] = usage_obj
            return f"data: {json.dumps(payload)}\n\n"

        yield chunk({"role": "assistant", "content": ""})

        # Tool-call streaming (test double): when the request carries tools, emit
        # OpenAI-shape per-index tool_call deltas (id+name on the first fragment,
        # arguments streamed incrementally) and a consolidated terminal tool_calls[]
        # with finish_reason="tool_calls" — mirroring the real providers' Component-6
        # aggregation so the chat path's streaming-correctness wiring can be exercised
        # without a live provider.
        if req.tools:
            tool_name = req.tools[0].function.name
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_mock_0",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": ""},
                        }
                    ]
                }
            )
            for frag in ('{"q":', ' "hi"', "}"):
                yield chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": frag}}]}
                )
            terminal_delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_mock_0",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": '{"q": "hi"}'},
                    }
                ]
            }
            yield chunk(terminal_delta, finish="tool_calls", usage_obj=usage.model_dump())
            yield "data: [DONE]\n\n"
            return

        for word in reply.split(" "):
            yield chunk({"content": word + " "})
        # Final event before [DONE] always carries usage (Component 6).
        yield chunk({}, finish="stop", usage_obj=usage.model_dump())
        yield "data: [DONE]\n\n"
