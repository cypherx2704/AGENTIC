"""Tool-calling emulation shim (small/non-native models)."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://llms_user:localdev@localhost:5432/cypherx_platform")

from llms_gateway.core.config import Settings  # noqa: E402
from llms_gateway.models.unified import (  # noqa: E402
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    FunctionDefinition,
    Message,
    ResponseMessage,
    Tool,
    Usage,
)
from llms_gateway.services import tool_emulation  # noqa: E402
from llms_gateway.services.providers.mock import MockProvider  # noqa: E402

_WEB_SEARCH = Tool(
    function=FunctionDefinition(
        name="web_search",
        description="Search the web.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
)


def _req(model: str, *, tools: bool = True, tool_mode: str = "auto", messages=None) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=messages or [Message(role="user", content="what is the weather in Paris?")],
        tools=[_WEB_SEARCH] if tools else None,
        tool_mode=tool_mode,
    )


# ── should_emulate decision matrix ─────────────────────────────────────────────────
def test_should_emulate_small_model_auto() -> None:
    s = Settings()
    assert tool_emulation.should_emulate(_req("llama-3.1-8b-instruct"), "llama-3.1-8b-instruct", s) is True


def test_should_not_emulate_frontier_model_auto() -> None:
    s = Settings()
    assert tool_emulation.should_emulate(_req("claude-sonnet-4-6"), "claude-sonnet-4-6", s) is False


def test_emulated_mode_forces_emulation_on_any_model() -> None:
    s = Settings()
    assert tool_emulation.should_emulate(
        _req("claude-sonnet-4-6", tool_mode="emulated"), "claude-sonnet-4-6", s
    ) is True


def test_native_mode_disables_emulation_on_small_model() -> None:
    s = Settings()
    assert tool_emulation.should_emulate(
        _req("llama-3.1-8b-instruct", tool_mode="native"), "llama-3.1-8b-instruct", s
    ) is False


def test_no_tools_never_emulates() -> None:
    s = Settings()
    assert tool_emulation.should_emulate(_req("llama-3.1-8b-instruct", tools=False), "llama-3.1-8b-instruct", s) is False


def test_unknown_model_follows_config_default() -> None:
    s = Settings()  # emulate_tools_when_unknown defaults False -> native
    assert tool_emulation.should_emulate(_req("some-custom-model"), "some-custom-model", s) is False


# ── request transform ───────────────────────────────────────────────────────────────
def test_build_emulated_request_strips_tools_and_injects_protocol() -> None:
    s = Settings()
    em = tool_emulation.build_emulated_request(_req("llama-3.1-8b-instruct"), s)
    assert em.tools is None and em.tool_choice is None
    system = "\n".join(m.content for m in em.messages if m.role == "system")
    assert tool_emulation.PROTOCOL_MARKER in system
    assert "web_search" in system  # the tool is described in the protocol


def test_build_flattens_tool_history() -> None:
    s = Settings()
    msgs = [
        Message(role="user", content="weather?"),
        Message(role="assistant", content="", tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"weather"}'}}
        ]),
        Message(role="tool", tool_call_id="call_1", name="web_search", content='{"results":"sunny"}'),
    ]
    em = tool_emulation.build_emulated_request(_req("llama-3.1-8b-instruct", messages=msgs), s)
    flat = "\n".join(m.content for m in em.messages)
    assert tool_emulation.TOOL_RESULT_PREFIX in flat  # tool result rendered as text
    assert "sunny" in flat
    # No message in the emulated request carries the native tool roles.
    assert all(m.role in ("system", "user", "assistant") for m in em.messages)


# ── response parsing ─────────────────────────────────────────────────────────────────
def test_extract_tool_call_bare_json() -> None:
    tc, preface = tool_emulation.extract_tool_call('{"tool_call": {"name": "web_search", "arguments": {"query": "x"}}}')
    assert tc == {"name": "web_search", "arguments": {"query": "x"}}
    assert preface == ""


def test_extract_tool_call_with_fences_and_prose() -> None:
    text = 'Sure, let me search.\n```json\n{"tool_call": {"name": "web_search", "arguments": {"query": "x"}}}\n```'
    tc, preface = tool_emulation.extract_tool_call(text)
    assert tc["name"] == "web_search"
    assert "search" in preface.lower()


def test_extract_tool_call_none_for_plain_text() -> None:
    tc, preface = tool_emulation.extract_tool_call("The weather in Paris is sunny.")
    assert tc is None
    assert preface == "The weather in Paris is sunny."


# ── end-to-end against the mock provider (keyless) ───────────────────────────────────
@pytest.mark.asyncio
async def test_run_emulated_chat_returns_tool_call_against_mock() -> None:
    s = Settings()
    resp = await tool_emulation.run_emulated_chat(
        MockProvider(), _req("llama-3.1-8b-instruct"), model_id="llama-3.1-8b-instruct", settings=s
    )
    msg = resp.choices[0].message
    assert resp.choices[0].finish_reason == "tool_calls"
    assert msg.tool_calls and msg.tool_calls[0].function.name == "web_search"


@pytest.mark.asyncio
async def test_run_emulated_chat_final_answer_after_tool_result() -> None:
    s = Settings()
    msgs = [
        Message(role="user", content="weather?"),
        Message(role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
        ]),
        Message(role="tool", tool_call_id="c1", name="web_search", content='{"results":"sunny"}'),
    ]
    resp = await tool_emulation.run_emulated_chat(
        MockProvider(), _req("llama-3.1-8b-instruct", messages=msgs), model_id="llama-3.1-8b-instruct", settings=s
    )
    # After a tool result, the mock emits a plain final answer (no tool call).
    assert resp.choices[0].finish_reason == "stop"
    assert not resp.choices[0].message.tool_calls
    assert json.loads  # sanity import use


# ── protocol-leak hardening: a flaky model must never leak the raw tool-call protocol ──
def _resp(content: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model="llama-3.1-8b-instruct",
        choices=[Choice(index=0, message=ResponseMessage(content=content), finish_reason="stop")],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
    )


def test_parse_valid_allowed_call_is_accepted() -> None:
    out = tool_emulation.parse_emulated_response(
        _resp('{"tool_call": {"name": "web_search", "arguments": {"query": "hi"}}}'), ["web_search"]
    )
    assert out.choices[0].finish_reason == "tool_calls"
    assert out.choices[0].message.tool_calls[0].function.name == "web_search"


def test_parse_malformed_protocol_is_stripped_not_leaked() -> None:
    # Truncated JSON (missing a closing brace) — the real 8B failure. Must NOT surface verbatim.
    leaked = '{"tool_call": {"name": "web_search", "arguments": {"query": "hi"}}'
    out = tool_emulation.parse_emulated_response(_resp(leaked), ["web_search"])
    assert not out.choices[0].message.tool_calls
    assert '"tool_call"' not in (out.choices[0].message.content or "")


def test_parse_disallowed_tool_name_is_stripped() -> None:
    out = tool_emulation.parse_emulated_response(
        _resp('{"tool_call": {"name": "not_a_real_tool", "arguments": {}}}'), ["web_search"]
    )
    assert not out.choices[0].message.tool_calls
    assert '"tool_call"' not in (out.choices[0].message.content or "")


def test_parse_plain_answer_is_untouched() -> None:
    out = tool_emulation.parse_emulated_response(_resp("Paris is sunny, 22C."), ["web_search"])
    assert out.choices[0].message.content == "Paris is sunny, 22C."
    assert not out.choices[0].message.tool_calls
