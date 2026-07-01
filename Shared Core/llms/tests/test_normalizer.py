"""Unit tests for the provider normalizer (pure functions, no IO)."""

from __future__ import annotations

import json

import pytest

from llms_gateway.core.errors import ApiError
from llms_gateway.models.unified import (
    ChatCompletionRequest,
    FunctionCall,
    FunctionDefinition,
    Message,
    ResponseFormat,
    Tool,
    ToolCall,
)
from llms_gateway.services import normalizer


def _req(**kw: object) -> ChatCompletionRequest:
    base: dict[str, object] = {
        "model": "claude-sonnet-4-6",
        "messages": [Message(role="user", content="hello")],
    }
    base.update(kw)
    return ChatCompletionRequest(**base)  # type: ignore[arg-type]


def test_system_messages_concatenated_into_anthropic_system() -> None:
    req = _req(
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="system", content="Be terse."),
            Message(role="user", content="hi"),
        ]
    )
    out = normalizer.to_anthropic(req)
    assert out["system"] == "You are helpful.\n\nBe terse."
    # system messages are removed from the messages array
    assert all(m["role"] != "system" for m in out["messages"])
    assert out["messages"][0]["role"] == "user"


def test_assistant_tool_calls_become_anthropic_tool_use_blocks() -> None:
    req = _req(
        messages=[
            Message(role="user", content="search"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        function=FunctionCall(name="web_search", arguments='{"query":"x"}'),
                    )
                ],
            ),
            Message(role="tool", tool_call_id="call_1", content="result-json"),
        ]
    )
    out = normalizer.to_anthropic(req)
    assistant = next(m for m in out["messages"] if m["role"] == "assistant")
    tool_use = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert tool_use["name"] == "web_search"
    assert tool_use["input"] == {"query": "x"}
    # tool role -> tool_result block on a user message
    tool_result_msg = out["messages"][-1]
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_tool_use_response_maps_to_unified_tool_calls() -> None:
    raw = {
        "id": "msg_1",
        "model": "claude-sonnet-4-6",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"query": "y"}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp = normalizer.from_anthropic(raw, request_model="claude-sonnet-4-6")
    choice = resp.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    tc = choice.message.tool_calls[0]
    assert tc.id == "tu_1"
    assert tc.function.name == "web_search"
    assert json.loads(tc.function.arguments) == {"query": "y"}


@pytest.mark.parametrize(
    ("anthropic_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
    ],
)
def test_stop_reason_map(anthropic_reason: str, expected: str) -> None:
    raw = {
        "id": "m",
        "model": "claude-haiku-4-5",
        "stop_reason": anthropic_reason,
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    resp = normalizer.from_anthropic(raw, request_model="claude-haiku-4-5")
    assert resp.choices[0].finish_reason == expected


def test_anthropic_cache_tokens_normalized() -> None:
    raw = {
        "id": "m",
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
        },
    }
    resp = normalizer.from_anthropic(raw, request_model="claude-sonnet-4-6")
    assert resp.usage.cached_prompt_tokens == 40
    assert resp.usage.cache_creation_tokens == 10


def test_temperature_clamped_for_anthropic() -> None:
    out = normalizer.to_anthropic(_req(temperature=1.8))
    assert out["temperature"] == 1.0


def test_parallel_tool_calls_false_disables_parallel_on_anthropic() -> None:
    req = _req(
        parallel_tool_calls=False,
        tools=[Tool(function=FunctionDefinition(name="f", parameters={"type": "object"}))],
    )
    out = normalizer.to_anthropic(req)
    assert out["tool_choice"]["disable_parallel_tool_use"] is True


def test_response_format_json_rejected_on_anthropic() -> None:
    req = _req(response_format=ResponseFormat(type="json_object"))
    with pytest.raises(ApiError) as exc:
        normalizer.to_anthropic(req)
    assert exc.value.code == "MODEL_UNSUPPORTED"
    assert exc.value.status_code == 422


def test_openai_streaming_forces_include_usage() -> None:
    req = _req(model="gpt-4o", stream=True)
    out = normalizer.to_openai(req)
    assert out["stream_options"]["include_usage"] is True


def test_openai_finish_reason_passthrough() -> None:
    raw = {
        "id": "cc",
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "length"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    resp = normalizer.from_openai(raw, request_model="gpt-4o")
    assert resp.choices[0].finish_reason == "length"
    assert resp.usage.total_tokens == 5
