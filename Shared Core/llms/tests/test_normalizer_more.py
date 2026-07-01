"""Additional normalizer edge cases (pure functions, no IO).

Covers cases beyond ``test_normalizer.py``: image_url content blocks, json_schema
response_format rejection on Anthropic, tool-role -> tool_result, multiple tool_calls
on a single assistant turn, and temperature clamping at the low end.
"""

from __future__ import annotations

import json

import pytest

from llms_gateway.core.errors import ApiError
from llms_gateway.models.unified import (
    ChatCompletionRequest,
    FunctionCall,
    ImageUrl,
    ImageUrlContent,
    Message,
    ResponseFormat,
    TextContent,
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


def test_image_url_content_block_becomes_anthropic_image_source() -> None:
    req = _req(
        messages=[
            Message(
                role="user",
                content=[
                    TextContent(type="text", text="What is in this image?"),
                    ImageUrlContent(
                        type="image_url",
                        image_url=ImageUrl(url="https://cdn.example.com/cat.png"),
                    ),
                ],
            )
        ]
    )
    out = normalizer.to_anthropic(req)
    user = out["messages"][0]
    assert user["role"] == "user"
    blocks = user["content"]
    text_block = next(b for b in blocks if b["type"] == "text")
    assert text_block["text"] == "What is in this image?"
    image_block = next(b for b in blocks if b["type"] == "image")
    assert image_block["source"]["type"] == "url"
    assert image_block["source"]["url"] == "https://cdn.example.com/cat.png"


def test_response_format_json_object_rejected_on_anthropic() -> None:
    req = _req(response_format=ResponseFormat(type="json_object"))
    with pytest.raises(ApiError) as exc:
        normalizer.to_anthropic(req)
    assert exc.value.code == "MODEL_UNSUPPORTED"
    assert exc.value.status_code == 422


def test_response_format_json_schema_rejected_on_anthropic() -> None:
    req = _req(
        response_format=ResponseFormat(
            type="json_schema",
            json_schema={"name": "out", "schema": {"type": "object"}},
        )
    )
    with pytest.raises(ApiError) as exc:
        normalizer.to_anthropic(req)
    assert exc.value.code == "MODEL_UNSUPPORTED"
    assert exc.value.status_code == 422


def test_tool_role_message_becomes_anthropic_tool_result() -> None:
    req = _req(
        messages=[
            Message(role="user", content="search"),
            Message(role="tool", tool_call_id="call_abc", content="the-tool-output"),
        ]
    )
    out = normalizer.to_anthropic(req)
    tool_msg = out["messages"][-1]
    # tool-role maps onto a user message carrying a tool_result block.
    assert tool_msg["role"] == "user"
    block = tool_msg["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_abc"
    assert block["content"] == "the-tool-output"


def test_multiple_tool_calls_round_trip_on_single_assistant_turn() -> None:
    req = _req(
        messages=[
            Message(role="user", content="do two things"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        function=FunctionCall(name="lookup", arguments='{"q":"a"}'),
                    ),
                    ToolCall(
                        id="call_2",
                        function=FunctionCall(name="search", arguments='{"q":"b"}'),
                    ),
                ],
            ),
        ]
    )
    out = normalizer.to_anthropic(req)
    assistant = next(m for m in out["messages"] if m["role"] == "assistant")
    tool_uses = [b for b in assistant["content"] if b["type"] == "tool_use"]
    assert len(tool_uses) == 2
    assert tool_uses[0]["id"] == "call_1"
    assert tool_uses[0]["name"] == "lookup"
    assert tool_uses[0]["input"] == {"q": "a"}
    assert tool_uses[1]["id"] == "call_2"
    assert tool_uses[1]["name"] == "search"
    assert tool_uses[1]["input"] == {"q": "b"}


def test_multiple_tool_use_response_maps_to_multiple_unified_tool_calls() -> None:
    raw = {
        "id": "msg_2",
        "model": "claude-sonnet-4-6",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "tu_a", "name": "lookup", "input": {"q": "a"}},
            {"type": "tool_use", "id": "tu_b", "name": "search", "input": {"q": "b"}},
        ],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    resp = normalizer.from_anthropic(raw, request_model="claude-sonnet-4-6")
    tcs = resp.choices[0].message.tool_calls
    assert tcs is not None
    assert [tc.id for tc in tcs] == ["tu_a", "tu_b"]
    assert json.loads(tcs[1].function.arguments) == {"q": "b"}


def test_temperature_low_end_passthrough_not_clamped() -> None:
    # Anthropic clamp only affects the high end (2 -> 1); a low value passes through.
    out = normalizer.to_anthropic(_req(temperature=0.2))
    assert out["temperature"] == pytest.approx(0.2)


def test_temperature_exactly_one_unchanged_for_anthropic() -> None:
    out = normalizer.to_anthropic(_req(temperature=1.0))
    assert out["temperature"] == 1.0


def test_temperature_above_one_clamped_to_one() -> None:
    out = normalizer.to_anthropic(_req(temperature=2.0))
    assert out["temperature"] == 1.0
