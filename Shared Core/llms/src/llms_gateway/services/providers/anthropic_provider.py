"""Anthropic provider adaptor (real SDK, behind a platform key).

Uses the normalizer to translate the unified request to Anthropic Messages API
kwargs and the response back. Tolerates a missing key by raising a clear 503 if
invoked without one.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

import structlog

from ...core.errors import ApiError, ErrorCode
from ...models.unified import ChatCompletionRequest, ChatCompletionResponse
from .. import normalizer
from .base import ProviderAdaptor

logger = structlog.get_logger(__name__)

# Anthropic stop_reason -> unified finish_reason (mirrors normalizer._ANTHROPIC_STOP_MAP).
_STREAM_STOP_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


class AnthropicProvider(ProviderAdaptor):
    provider = "anthropic"

    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key

    def with_api_key(self, api_key: str) -> AnthropicProvider:
        """Return a lightweight clone bound to ``api_key`` (BYOK per-call key injection).

        The SDK client is built lazily per call, so a fresh adaptor is cheap and the
        shared platform-keyed instance is never mutated.
        """
        return AnthropicProvider(api_key)

    def _client(self):  # type: ignore[no-untyped-def]
        if not self._api_key:
            raise ApiError(
                ErrorCode.SERVICE_UNAVAILABLE,
                "Anthropic provider is not configured (missing ANTHROPIC_API_KEY).",
            )
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=self._api_key)

    async def chat(self, req: ChatCompletionRequest, *, model_id: str) -> ChatCompletionResponse:
        client = self._client()
        kwargs = normalizer.to_anthropic(req)
        kwargs["model"] = model_id
        try:
            message = await client.messages.create(**kwargs)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 — provider/network failures
            logger.warning("anthropic_call_failed", error=str(exc))
            raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Anthropic provider call failed.") from exc
        raw = message.model_dump() if hasattr(message, "model_dump") else dict(message)
        return normalizer.from_anthropic(raw, request_model=model_id)

    async def chat_stream(
        self, req: ChatCompletionRequest, *, model_id: str
    ) -> AsyncIterator[str]:
        client = self._client()
        kwargs = normalizer.to_anthropic(req)
        kwargs["model"] = model_id
        resp_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        input_tokens = 0
        output_tokens = 0
        cached_prompt_tokens = 0
        cache_creation_tokens = 0
        finish_reason = "stop"

        # Tool-call aggregation: Anthropic streams a tool_use block as a
        # content_block_start (carrying id + name) followed by N input_json_delta
        # fragments (partial_json). We accumulate per block index, then emit ONE
        # consolidated tool_calls[] in the terminal event (Component 6).
        tool_blocks: dict[int, dict] = {}  # index -> {"id", "name", "args": [fragments]}

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

        try:
            async with client.messages.stream(**kwargs) as stream:
                yield chunk({"role": "assistant", "content": ""})
                async for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "message_start":
                        usage = getattr(getattr(event, "message", None), "usage", None)
                        if usage is not None:
                            input_tokens = getattr(usage, "input_tokens", 0) or 0
                            # Cache-token normalization (Component 6): map Anthropic's
                            # cache_read/cache_creation onto the unified usage fields.
                            cached_prompt_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
                            cache_creation_tokens = (
                                getattr(usage, "cache_creation_input_tokens", 0) or 0
                            )
                    elif etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", "") == "tool_use":
                            idx = getattr(event, "index", len(tool_blocks))
                            tool_blocks[idx] = {
                                "id": getattr(block, "id", "") or "",
                                "name": getattr(block, "name", "") or "",
                                "args": [],
                            }
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        text = getattr(delta, "text", None)
                        if text:
                            yield chunk({"content": text})
                        partial = getattr(delta, "partial_json", None)
                        if partial:
                            idx = getattr(event, "index", None)
                            entry = tool_blocks.get(idx) if idx is not None else None
                            if entry is not None:
                                entry["args"].append(partial)
                    elif etype == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage is not None:
                            output_tokens = (
                                getattr(usage, "output_tokens", output_tokens) or output_tokens
                            )
                        # finish_reason normalization from the terminal stop_reason.
                        delta = getattr(event, "delta", None)
                        stop_reason = getattr(delta, "stop_reason", None)
                        if stop_reason:
                            finish_reason = _STREAM_STOP_MAP.get(stop_reason, "stop")
        except Exception as exc:  # noqa: BLE001 — mid-stream provider error
            logger.warning("anthropic_stream_failed", error=str(exc))
            err = {"error": {"code": ErrorCode.SERVICE_UNAVAILABLE, "message": "Anthropic stream failed."}}
            yield f"event: error\ndata: {json.dumps(err)}\n\n"
            return

        from ..cost import cost_calculator

        cost = cost_calculator.compute(
            self.provider,
            model_id,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        usage_obj = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cached_prompt_tokens": cached_prompt_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cost_usd": cost,
        }
        # Consolidated tool_calls[] in the terminal delta (OpenAI shape) if any.
        terminal_delta: dict = {}
        if tool_blocks:
            terminal_delta["tool_calls"] = [
                {
                    "index": i,
                    "id": tb["id"],
                    "type": "function",
                    "function": {"name": tb["name"], "arguments": "".join(tb["args"])},
                }
                for i, (_idx, tb) in enumerate(sorted(tool_blocks.items()))
            ]
            if finish_reason == "stop":
                finish_reason = "tool_calls"
        yield chunk(terminal_delta, finish=finish_reason, usage_obj=usage_obj)
        yield "data: [DONE]\n\n"
