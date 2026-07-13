"""Unified <-> provider request/response normalization (Component 1 & 3).

Pure functions (no IO) translating the unified OpenAI-superset schema to/from the
Anthropic Messages API and the OpenAI Chat Completions API.

Mandatory Anthropic normalizations implemented here:
  * system-role messages concatenated into the Anthropic top-level ``system`` field.
  * assistant ``tool_calls`` -> Anthropic ``tool_use`` content blocks.
  * tool-role messages -> Anthropic ``tool_result`` content blocks.
  * Anthropic ``tool_use`` response content blocks -> unified ``tool_calls[]``.
  * stop_reason map: end_turn/stop_sequence -> stop, tool_use -> tool_calls,
    max_tokens -> length, refusal -> content_filter.
  * cached tokens: cache_read_input_tokens -> cached_prompt_tokens,
    cache_creation_input_tokens -> cache_creation_tokens.
  * temperature clamp [0,2] -> [0,1].
  * parallel_tool_calls -> disable_parallel_tool_use = !parallel_tool_calls.
  * response_format json_object/json_schema -> 422 MODEL_UNSUPPORTED (no fallback).

OpenAI: largely pass-through; finish_reason passes through; streaming forces
``stream_options.include_usage = true``.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import ApiError, ErrorCode
from ..models.unified import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    FinishReason,
    FunctionCall,
    ImageUrlContent,
    Message,
    NamedToolChoice,
    ResponseMessage,
    TextContent,
    ToolCall,
    Usage,
)

# Anthropic stop_reason -> unified finish_reason.
_ANTHROPIC_STOP_MAP: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


# ── helpers ─────────────────────────────────────────────────────────────────────
def _content_to_text(content: str | list[Any] | None) -> str:
    """Flatten a message content (str | content-parts) to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, TextContent):
            parts.append(part.text)
    return "".join(parts)


# ── Anthropic ─────────────────────────────────────────────────────────────────
def to_anthropic(req: ChatCompletionRequest) -> dict[str, Any]:
    """Translate a unified request into Anthropic Messages API kwargs."""
    if req.response_format is not None and req.response_format.type in ("json_object", "json_schema"):
        raise ApiError(
            ErrorCode.MODEL_UNSUPPORTED,
            "Structured output (response_format json_object/json_schema) is not supported on "
            "Anthropic models. Supported on the OpenAI gpt-4o family.",
            status_code=422,
            details={"supported_models": ["gpt-4o", "gpt-4o-mini"]},
        )

    system_chunks: list[str] = []
    messages: list[dict[str, Any]] = []

    for msg in req.messages:
        if msg.role == "system":
            system_chunks.append(_content_to_text(msg.content))
            continue
        if msg.role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": _content_to_text(msg.content),
                        }
                    ],
                }
            )
            continue
        if msg.role == "assistant":
            content_blocks: list[dict[str, Any]] = []
            text = _content_to_text(msg.content)
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in msg.tool_calls or []:
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": _safe_json(tc.function.arguments),
                    }
                )
            messages.append({"role": "assistant", "content": content_blocks})
            continue
        # user role
        messages.append({"role": "user", "content": _user_content_to_anthropic(msg)})

    out: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        # Anthropic requires max_tokens; default to a sane value if absent.
        "max_tokens": req.max_tokens or 1024,
    }
    if system_chunks:
        out["system"] = "\n\n".join(c for c in system_chunks if c)
    if req.temperature is not None:
        out["temperature"] = min(req.temperature, 1.0)  # clamp [0,2] -> [0,1]
    if req.top_p is not None:
        out["top_p"] = req.top_p
    if req.stop is not None:
        out["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else req.stop
    if req.tools:
        out["tools"] = [
            {
                "name": t.function.name,
                "description": t.function.description or "",
                "input_schema": t.function.parameters or {"type": "object", "properties": {}},
            }
            for t in req.tools
        ]
        out["tool_choice"] = _to_anthropic_tool_choice(req)
        if not req.parallel_tool_calls and isinstance(out["tool_choice"], dict):
            out["tool_choice"]["disable_parallel_tool_use"] = True
    return out


def _user_content_to_anthropic(msg: Message) -> list[dict[str, Any]] | str:
    if isinstance(msg.content, str) or msg.content is None:
        return _content_to_text(msg.content)
    blocks: list[dict[str, Any]] = []
    for part in msg.content:
        if isinstance(part, TextContent):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImageUrlContent):
            # Multimodal SSRF-hardened fetch+rebase64 is deferred; pass a URL source
            # block so the schema is open. Anthropic accepts known-CDN URLs.
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "url", "url": part.image_url.url},
                }
            )
    return blocks


def _to_anthropic_tool_choice(req: ChatCompletionRequest) -> dict[str, Any]:
    tc = req.tool_choice
    if tc is None or tc == "auto":
        return {"type": "auto"}
    if tc == "none":
        return {"type": "none"}
    if tc == "required":
        return {"type": "any"}
    if isinstance(tc, NamedToolChoice):
        return {"type": "tool", "name": tc.function.name}
    return {"type": "auto"}


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {}


def from_anthropic(resp: dict[str, Any], *, request_model: str) -> ChatCompletionResponse:
    """Translate an Anthropic Messages API response into a unified response.

    ``usage`` cost_usd is left at 0.0 here; the caller computes cost from the
    normalized token counts via :mod:`cost`.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in resp.get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    function=FunctionCall(
                        name=block.get("name", ""),
                        arguments=json.dumps(block.get("input", {})),
                    ),
                )
            )

    stop_reason = resp.get("stop_reason")
    finish_reason: FinishReason = _ANTHROPIC_STOP_MAP.get(stop_reason or "", "stop")

    raw_usage = resp.get("usage", {}) or {}
    prompt = int(raw_usage.get("input_tokens", 0))
    completion = int(raw_usage.get("output_tokens", 0))
    cached = int(raw_usage.get("cache_read_input_tokens", 0))
    creation = int(raw_usage.get("cache_creation_input_tokens", 0))

    message = ResponseMessage(
        content="".join(text_parts) or None,
        tool_calls=tool_calls or None,
    )
    return ChatCompletionResponse(
        id=resp.get("id") or f"chatcmpl-{request_model}",
        model=resp.get("model", request_model),
        choices=[Choice(index=0, message=message, finish_reason=finish_reason)],
        usage=Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cached_prompt_tokens=cached,
            cache_creation_tokens=creation,
        ),
    )


# ── OpenAI ────────────────────────────────────────────────────────────────────
def to_openai(req: ChatCompletionRequest) -> dict[str, Any]:
    """Translate a unified request into OpenAI Chat Completions kwargs.

    Mostly a pass-through of the OpenAI-shaped schema. On streaming calls
    ``stream_options.include_usage`` is forced true so usage arrives in the final
    chunk (server wins on include_usage).
    """
    payload: dict[str, Any] = req.model_dump(exclude_none=True, by_alias=True)
    if req.stream:
        so = payload.get("stream_options") or {}
        so["include_usage"] = True
        # aggregate_tool_calls is a gateway-internal flag; drop before sending upstream.
        so.pop("aggregate_tool_calls", None)
        payload["stream_options"] = so
    else:
        payload.pop("stream_options", None)
    # Gateway-internal fields not understood by the OpenAI SDK / OpenAI-compatible
    # providers (OpenRouter, Together, Groq, vLLM, …). These are consumed by the gateway
    # itself (routing, tool-emulation, metering) and MUST be stripped before the upstream
    # `create(**kwargs)` call — otherwise the SDK raises
    # "unexpected keyword argument '<field>'".
    for internal_field in ("metadata", "tool_mode"):
        payload.pop(internal_field, None)
    return payload


def from_openai(resp: dict[str, Any], *, request_model: str) -> ChatCompletionResponse:
    """Translate an OpenAI Chat Completions response into a unified response.

    finish_reason passes through. cost_usd computed by the caller.
    """
    choices: list[Choice] = []
    for ch in resp.get("choices", []) or []:
        msg = ch.get("message", {}) or {}
        tcs: list[ToolCall] = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            tcs.append(
                ToolCall(
                    id=tc.get("id", ""),
                    function=FunctionCall(name=fn.get("name", ""), arguments=fn.get("arguments", "")),
                )
            )
        choices.append(
            Choice(
                index=ch.get("index", 0),
                message=ResponseMessage(content=msg.get("content"), tool_calls=tcs or None),
                finish_reason=ch.get("finish_reason"),
            )
        )

    raw_usage = resp.get("usage", {}) or {}
    prompt = int(raw_usage.get("prompt_tokens", 0))
    completion = int(raw_usage.get("completion_tokens", 0))
    return ChatCompletionResponse(
        id=resp.get("id") or f"chatcmpl-{request_model}",
        model=resp.get("model", request_model),
        choices=choices,
        usage=Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(raw_usage.get("total_tokens", prompt + completion)),
            cached_prompt_tokens=0,  # OpenAI: no public cache-token reporting yet
            cache_creation_tokens=0,
        ),
    )
