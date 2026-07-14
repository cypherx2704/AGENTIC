"""Tool-calling EMULATION shim — make every model (incl. small/8B) use tools.

The frontier models expose a native ``tools[]`` function-calling API: the caller passes
tool schemas, the model replies with structured ``message.tool_calls``. Small open models
(≈7-8B) either lack that API or use it unreliably, so a caller that depends on native
``tool_calls`` silently never invokes a tool against them.

This module closes that gap WITHOUT changing the public contract. When a model is not
natively tool-capable (``model_capabilities.native_tool_use=false``, or the request asks
for ``tool_mode="emulated"``), the gateway:

  1. REMOVES ``tools``/``tool_choice`` from the provider request and instead injects a
     synthetic system message that lists the tools + a STRICT tool-call protocol (reply
     with one JSON object to call a tool, or plain text for the final answer).
  2. FLATTENS the OpenAI tool-calling history (assistant ``tool_calls`` turns + ``tool``
     -role results) into plain text a small model reliably reads — so multi-turn tool
     loops work even though the provider never sees the tool roles.
  3. Calls the provider as a PLAIN chat and PARSES the model's text reply back into
     normalized ``message.tool_calls`` + ``finish_reason="tool_calls"`` (or leaves it as a
     plain ``stop`` answer).

The gateway stays STATELESS per call — the actual invoke→feed-result→ask-again loop lives
in the caller (xAgent's TOOL_LOOP). Each follow-up call re-flattens the updated history.

Transparent to callers: xAgent's existing tool loop sends ``tools`` and reads
``completion.tool_calls`` exactly as before; emulation is selected by capability + the
``tool_mode`` request field, and surfaced via the ``X-Cypherx-Tool-Mode`` response header.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

import structlog

from ..models.unified import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    FunctionCall,
    Message,
    TextContent,
    ToolCall,
)
from .capabilities import capability_registry

if TYPE_CHECKING:
    from ..core.config import Settings
    from .providers.base import ProviderAdaptor

logger = structlog.get_logger(__name__)

# Stable marker embedded at the top of the injected protocol system message. Lets the
# deterministic mock provider detect emulated mode (so the keyless local path can exercise
# an end-to-end emulated tool call). Real models simply read it as part of the instructions.
PROTOCOL_MARKER = "[[cypherx-tool-protocol/v1]]"
# Prefix used when flattening a `tool`-role result into plain text (also the mock's signal
# that a tool has already run -> emit a final answer rather than another tool call).
TOOL_RESULT_PREFIX = "TOOL RESULT"


# ── decision ─────────────────────────────────────────────────────────────────────
def should_emulate(body: ChatCompletionRequest, model_id: str, settings: Settings) -> bool:
    """Decide whether to EMULATE tool-calling for this request.

    * No tools on the request           -> never (nothing to emulate).
    * master switch off                  -> never.
    * tool_mode == "native"              -> never.
    * tool_mode == "emulated"            -> always.
    * tool_mode == "auto" (the default)  -> emulate iff the model is NOT known-native; an unknown
      model follows ``emulate_tools_when_unknown`` (default: EMULATE — see below).

    NOTE an explicit ``native``/``emulated`` short-circuits BEFORE the capability lookup, pinning one
    mode for every model the caller sends. That is almost never what you want: the mode is a property
    of the MODEL. Leave ``tool_mode`` at "auto" and let this function derive it per model.

    The unknown-model default is EMULATE because the two mistakes are not symmetric: emulating a
    model that could have gone native costs a few prompt tokens, whereas driving a model natively
    that cannot do it is a hard failure (Groq answers 400 ``tool_use_failed`` and the caller's whole
    task dies). Unknown is also the common case — tenant BYOK models arrive with no capability row.
    """
    if not body.tools:
        return False
    if not getattr(settings, "tool_emulation_enabled", True):
        return False
    mode = getattr(body, "tool_mode", "auto") or "auto"
    if mode == "native":
        return False
    if mode == "emulated":
        return True
    # auto
    native = capability_registry.native_tool_use(model_id)
    if native is None:
        # Default True — and it MUST match Settings.emulate_tools_when_unknown. A getattr default
        # is a second place the policy is written down; if the two ever disagree, an object without
        # the attribute silently reverts to the OLD unsafe behaviour (native-on-unknown).
        return bool(getattr(settings, "emulate_tools_when_unknown", True))
    return native is False


# ── request transform (tools -> prompt protocol; flatten tool history) ─────────────
def _flatten_content(content: Any) -> str:
    """Render a message's content (str | list[ContentPart] | None) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for p in content:
        if isinstance(p, TextContent):
            parts.append(p.text)
        else:  # image_url or unknown part — represent compactly so context is preserved
            parts.append("[image]")
    return "".join(parts)


def _tool_catalog(body: ChatCompletionRequest, max_tools: int) -> tuple[str, list[str]]:
    """Render the offered tools into a numbered catalog + return their names (capped)."""
    tools = (body.tools or [])[:max_tools]
    names: list[str] = []
    lines: list[str] = []
    for i, tool in enumerate(tools, start=1):
        fn = tool.function
        names.append(fn.name)
        schema = json.dumps(fn.parameters or {"type": "object"}, ensure_ascii=False)
        lines.append(f"{i}. {fn.name} — {fn.description or 'no description'}\n   arguments JSON schema: {schema}")
    return "\n".join(lines), names


def _protocol_message(catalog: str, names: list[str]) -> Message:
    """Build the system message that teaches a non-native model the tool-call protocol."""
    content = (
        f"{PROTOCOL_MARKER}\n"
        "You can call external tools to gather information or take actions before "
        "answering. Available tools:\n\n"
        f"{catalog}\n\n"
        f"Tool names: {', '.join(names)}\n\n"
        "To CALL a tool, reply with ONLY a single JSON object and NOTHING else "
        "(no prose, no markdown code fences), in EXACTLY this form:\n"
        '{"tool_call": {"name": "<one of the tool names above>", "arguments": { ... }}}\n\n'
        "The \"arguments\" object MUST satisfy that tool's arguments JSON schema. Call ONE "
        f"tool at a time. After each call you will receive a message beginning with "
        f"\"{TOOL_RESULT_PREFIX}\" containing the tool's output. When you have gathered "
        "enough information, reply with your FINAL answer as plain text — do NOT wrap the "
        "final answer in JSON and do NOT emit a tool_call. Never invent tool output."
    )
    return Message(role="system", content=content)


def _flatten_history(messages: list[Message]) -> tuple[list[Message], list[Message]]:
    """Split into (system_messages, flattened_dialogue) for the emulated request.

    Assistant turns carrying ``tool_calls`` and ``tool``-role results are rewritten into
    plain user/assistant text so a model called WITHOUT the native tools API can still
    follow a multi-turn tool loop.
    """
    systems: list[Message] = []
    dialogue: list[Message] = []
    for m in messages:
        if m.role == "system":
            systems.append(Message(role="system", content=_flatten_content(m.content)))
        elif m.role == "tool":
            label = m.name or m.tool_call_id or "tool"
            dialogue.append(
                Message(role="user", content=f"{TOOL_RESULT_PREFIX} ({label}): {_flatten_content(m.content)}")
            )
        elif m.role == "assistant" and m.tool_calls:
            text = _flatten_content(m.content)
            calls = "; ".join(
                f"{c.function.name}({c.function.arguments})" for c in m.tool_calls
            )
            rendered = (f"{text}\n" if text else "") + f"(I called: {calls})"
            dialogue.append(Message(role="assistant", content=rendered))
        else:
            dialogue.append(Message(role=m.role, content=_flatten_content(m.content)))
    return systems, dialogue


def build_emulated_request(body: ChatCompletionRequest, settings: Settings) -> ChatCompletionRequest:
    """Return a copy of ``body`` with tools removed and the tool protocol spliced in."""
    catalog, names = _tool_catalog(body, getattr(settings, "tool_emulation_max_tools", 16))
    systems, dialogue = _flatten_history(list(body.messages))
    new_messages = [*systems, _protocol_message(catalog, names), *dialogue]
    return body.model_copy(
        update={
            "messages": new_messages,
            "tools": None,
            "tool_choice": None,
            "tool_mode": "native",  # the provider call itself is a plain chat
        }
    )


# ── response parse (model text -> normalized tool_calls) ───────────────────────────
def _iter_json_objects(text: str) -> Iterator[str]:
    """Yield each balanced top-level ``{...}`` substring (string-literal aware)."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : i + 1]


def _as_tool_call(obj: Any) -> dict[str, Any] | None:
    """Normalize a parsed JSON object into ``{name, arguments}`` if it is a tool call."""
    if not isinstance(obj, dict):
        return None
    inner = obj.get("tool_call") if isinstance(obj.get("tool_call"), dict) else obj
    name = inner.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = inner.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return {"name": name, "arguments": args}


def extract_tool_call(text: str | None) -> tuple[dict[str, Any] | None, str]:
    """Find a tool-call JSON in free model output. Returns (tool_call | None, preface_text).

    Tolerant of markdown fences and prose around the JSON. The preface is the text BEFORE
    the matched JSON object (trimmed) — surfaced as the assistant message content.
    """
    if not text:
        return None, ""
    for candidate in _iter_json_objects(text):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        tc = _as_tool_call(parsed)
        if tc is not None:
            preface = text.split(candidate, 1)[0].strip().strip("`").strip()
            return tc, preface
    return None, text.strip()


# Shown to the user only when a model's final answer was itself a broken/rejected tool-call
# protocol attempt (so the raw ``{"tool_call": ...}`` never surfaces as the answer).
_EMULATION_FALLBACK = "I couldn't complete that with the available tools — please rephrase or try again."


def _strip_tool_call_protocol(text: str) -> str:
    """Drop a leaked ``{...\"tool_call\"...}`` protocol block, keeping any prose before it.

    A flaky (usually small ~8B) model sometimes echoes the tool-call protocol as its FINAL answer,
    or malforms/truncates the JSON so it can't be parsed into a real tool call. Either way the raw
    protocol must never reach the user as the answer — keep the leading prose, drop the JSON block.
    """
    brace = text.find("{")
    return text[:brace].strip() if brace != -1 else text.strip()


def parse_emulated_response(
    response: ChatCompletionResponse, allowed_names: list[str]
) -> ChatCompletionResponse:
    """Rewrite a plain completion into a tool-call completion when the text requests one.

    A tool call is accepted only when its name is in ``allowed_names`` (the offered tools),
    so a hallucinated tool name falls through as a normal text answer instead of driving a
    wasted loop iteration. If the text was a tool-call protocol ATTEMPT that we could NOT accept
    (malformed/truncated JSON, or a disallowed tool name), the raw protocol is stripped so it can
    never leak to the user as the final answer.
    """
    if not response.choices:
        return response
    choice = response.choices[0]
    content = choice.message.content
    tc, preface = extract_tool_call(content)
    if tc is not None and (not allowed_names or tc["name"] in allowed_names):
        call = ToolCall(
            id=f"call_{uuid.uuid4().hex[:24]}",
            function=FunctionCall(name=tc["name"], arguments=json.dumps(tc["arguments"], ensure_ascii=False)),
        )
        choice.message.tool_calls = [call]
        choice.message.content = preface or None
        choice.finish_reason = "tool_calls"
        logger.info("tool_emulation_parsed_call", tool=tc["name"])
        return response
    # Not a usable tool call. If the model was ATTEMPTING the protocol (the marker survives in the
    # text), strip the protocol JSON so a malformed/truncated call or a disallowed tool name can't
    # surface verbatim as the answer (finding: an 8B emulated model leaked `{"tool_call": ...}`).
    if content and '"tool_call"' in content:
        cleaned = (preface if tc is not None else _strip_tool_call_protocol(content)).strip()
        choice.message.content = cleaned or _EMULATION_FALLBACK
        logger.warning("tool_emulation_protocol_leak_suppressed", preview=content[:120])
    return response


# ── orchestration ──────────────────────────────────────────────────────────────────
async def run_emulated_chat(
    provider: ProviderAdaptor, body: ChatCompletionRequest, *, model_id: str, settings: Settings
) -> ChatCompletionResponse:
    """Non-streaming emulated completion: transform -> provider.chat -> parse tool call."""
    allowed = [t.function.name for t in (body.tools or [])]
    em_req = build_emulated_request(body, settings)
    response = await provider.chat(em_req, model_id=model_id)
    return parse_emulated_response(response, allowed)


async def emulated_chat_stream(
    provider: ProviderAdaptor, body: ChatCompletionRequest, *, model_id: str, settings: Settings
) -> AsyncIterator[str]:
    """Streamed emulated completion: buffer a non-stream emulated call, emit it as SSE.

    Emulation needs the WHOLE reply to parse a tool call, so it cannot stream token-by
    -token; it instead emits one consolidated set of SSE frames (role, content/tool_calls,
    a terminal usage chunk, then ``[DONE]``) — byte-compatible with the chat path's stream
    consumer (which only needs the terminal usage chunk for billing).
    """
    import time

    response = await run_emulated_chat(provider, body, model_id=model_id, settings=settings)
    choice = response.choices[0] if response.choices else None
    resp_id = response.id
    created = int(time.time())

    def frame(delta: dict[str, Any], finish: str | None = None, usage: dict | None = None) -> str:
        payload: dict[str, Any] = {
            "id": resp_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            payload["usage"] = usage
        return f"data: {json.dumps(payload)}\n\n"

    yield frame({"role": "assistant", "content": ""})
    if choice is None:
        yield frame({}, finish="stop", usage=response.usage.model_dump())
        yield "data: [DONE]\n\n"
        return
    msg = choice.message
    if msg.tool_calls:
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in msg.tool_calls
            ]
        }
        yield frame(delta, finish="tool_calls", usage=response.usage.model_dump())
    else:
        yield frame({"content": msg.content or ""}, finish=choice.finish_reason or "stop",
                    usage=response.usage.model_dump())
    yield "data: [DONE]\n\n"
