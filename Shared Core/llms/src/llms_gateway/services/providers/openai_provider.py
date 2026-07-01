"""OpenAI provider adaptor (real SDK, behind a platform key).

Pass-through translation via the normalizer; forces ``stream_options.include_usage``
on streaming calls. Tolerates a missing key by raising a clear 503 if invoked.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

import structlog

from ...core.errors import ApiError, ErrorCode
from ...models.unified import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingUsage,
)
from .. import normalizer
from .base import ProviderAdaptor

logger = structlog.get_logger(__name__)


class OpenAIProvider(ProviderAdaptor):
    provider = "openai"

    def __init__(self, api_key: str | None, base_url: str | None = None) -> None:
        self._api_key = api_key
        # Optional OpenAI-compatible base_url (OpenRouter, Together, Groq, vLLM, Ollama,
        # self-hosted, …). None => the SDK default (api.openai.com). This is what makes the
        # adaptor work for ANY OpenAI-compatible provider with no per-provider code.
        self._base_url = base_url

    def with_api_key(self, api_key: str) -> OpenAIProvider:
        """Return a lightweight clone bound to ``api_key`` (BYOK per-call key injection)."""
        return OpenAIProvider(api_key, self._base_url)

    def with_credentials(self, api_key: str, base_url: str | None = None) -> OpenAIProvider:
        """Clone bound to a BYOK ``api_key`` AND a per-connection ``base_url`` (OpenAI-compatible)."""
        return OpenAIProvider(api_key, base_url)

    def _client(self):  # type: ignore[no-untyped-def]
        if not self._api_key:
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                "OpenAI-compatible provider is not configured (no API key for this connection).",
            )
        from openai import AsyncOpenAI

        if self._base_url:
            return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        return AsyncOpenAI(api_key=self._api_key)

    async def chat(self, req: ChatCompletionRequest, *, model_id: str) -> ChatCompletionResponse:
        client = self._client()
        kwargs = normalizer.to_openai(req)
        kwargs["model"] = model_id
        kwargs.pop("stream", None)
        try:
            completion = await client.chat.completions.create(**kwargs)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 — provider/network failures
            logger.warning("openai_call_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "OpenAI provider call failed.") from exc
        raw = completion.model_dump() if hasattr(completion, "model_dump") else dict(completion)
        return normalizer.from_openai(raw, request_model=model_id)

    async def embed(self, req: EmbeddingRequest, *, model_id: str) -> EmbeddingResponse:
        client = self._client()
        # Only forward params the caller actually set — the SDK uses sentinels for
        # unset optionals (passing dimensions=None to text-embedding-ada-* errors).
        kwargs: dict = {"input": req.input, "model": model_id}
        if req.dimensions is not None:
            kwargs["dimensions"] = req.dimensions
        if req.encoding_format is not None:
            kwargs["encoding_format"] = req.encoding_format
        if req.user is not None:
            kwargs["user"] = req.user
        try:
            result = await client.embeddings.create(**kwargs)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 — provider/network failures
            logger.warning("openai_embed_failed", error=str(exc))
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE, "OpenAI embeddings call failed."
            ) from exc
        raw = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        raw_usage = raw.get("usage", {}) or {}
        prompt = int(raw_usage.get("prompt_tokens", 0))
        total = int(raw_usage.get("total_tokens", prompt))
        return EmbeddingResponse(
            model=raw.get("model", model_id),
            data=[
                EmbeddingData(
                    embedding=list(d.get("embedding", [])),
                    index=int(d.get("index", i)),
                )
                for i, d in enumerate(raw.get("data", []) or [])
            ],
            usage=EmbeddingUsage(prompt_tokens=prompt, total_tokens=total),
        )

    async def chat_stream(
        self, req: ChatCompletionRequest, *, model_id: str
    ) -> AsyncIterator[str]:
        client = self._client()
        kwargs = normalizer.to_openai(req)
        kwargs["model"] = model_id
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
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

        prompt_tokens = 0
        completion_tokens = 0
        finish_reason: str | None = "stop"
        # Tool-call aggregation: OpenAI streams tool_calls as deltas keyed by `index`,
        # with the id + function.name on the first fragment and function.arguments
        # streamed incrementally. We accumulate per index and emit ONE consolidated
        # tool_calls[] in the terminal event (Component 6). The per-fragment deltas are
        # still forwarded so a client streaming tool args incrementally keeps working.
        tool_acc: dict[int, dict] = {}  # index -> {"id", "name", "args": [fragments]}
        try:
            stream = await client.chat.completions.create(**kwargs)
            async for event in stream:
                raw = event.model_dump() if hasattr(event, "model_dump") else dict(event)
                usage = raw.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                for ch in raw.get("choices", []) or []:
                    delta = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                    for tc in delta.get("tool_calls", []) or []:
                        idx = tc.get("index", 0)
                        entry = tool_acc.setdefault(idx, {"id": "", "name": "", "args": []})
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            entry["name"] = fn["name"]
                        if fn.get("arguments"):
                            entry["args"].append(fn["arguments"])
                    if delta:
                        yield chunk(delta)
        except Exception as exc:  # noqa: BLE001 — mid-stream provider error
            logger.warning("openai_stream_failed", error=str(exc))
            err = {"error": {"code": ErrorCode.SERVICE_UNAVAILABLE, "message": "OpenAI stream failed."}}
            yield f"event: error\ndata: {json.dumps(err)}\n\n"
            return

        from ..cost import cost_calculator

        cost = cost_calculator.compute(
            self.provider, model_id, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        usage_obj = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cached_prompt_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": cost,
        }
        # Consolidated tool_calls[] in the terminal delta if the model called tools.
        terminal_delta: dict = {}
        if tool_acc:
            terminal_delta["tool_calls"] = [
                {
                    "index": i,
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": "".join(tc["args"])},
                }
                for i, (_idx, tc) in enumerate(sorted(tool_acc.items()))
            ]
            if finish_reason in (None, "stop"):
                finish_reason = "tool_calls"
        yield chunk(terminal_delta, finish=finish_reason, usage_obj=usage_obj)
        yield "data: [DONE]\n\n"
