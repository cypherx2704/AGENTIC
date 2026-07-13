"""MCP (Model Context Protocol) JSON-RPC 2.0 message builders — the real-MCP wire.

Pure protocol layer behind ``POST /mcp`` (the spec-compliant Streamable-HTTP endpoint). It
builds the JSON-RPC 2.0 envelopes MCP mandates and knows nothing about auth or the backend,
so it is unit-testable with plain dicts. The router (``api/mcp.py``) owns governance and
calls these builders to shape responses.

Wire (MCP 2025-06-18): ``initialize`` -> ``{protocolVersion, capabilities, serverInfo}``;
``tools/list`` -> ``{tools:[...]}``; ``tools/call`` -> ``{content, isError}`` (execution
errors are ``isError: true`` results carrying a platform ``_meta`` with ``code`` +
``retryable``); protocol-level failures use JSON-RPC ``error`` objects.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL_VERSION = "2025-06-18"

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def negotiate_protocol_version(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PREFERRED_PROTOCOL_VERSION


def result_message(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}


def error_message(
    msg_id: Any, code: int, message: str, *, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": err}


def initialize_result(
    requested_version: Any, *, server_name: str, server_version: str, instructions: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocolVersion": negotiate_protocol_version(requested_version),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": server_name, "version": server_version},
    }
    if instructions:
        result["instructions"] = instructions
    return result


def tools_list_result(manifest_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Map Contract-4 ``manifest.tools[]`` to MCP ``tools/list`` entries (camelCase schemas)."""
    tools: list[dict[str, Any]] = []
    for t in manifest_tools:
        entry: dict[str, Any] = {
            "name": t["name"],
            "description": t.get("description", ""),
            "inputSchema": t.get("input_schema") or {"type": "object"},
        }
        if t.get("output_schema"):
            entry["outputSchema"] = t["output_schema"]
        tools.append(entry)
    return {"tools": tools}


def tool_success(text: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def tool_error(
    message: str, *, code: str, retryable: bool, pointer: str | None = None
) -> dict[str, Any]:
    meta: dict[str, Any] = {"code": code, "retryable": retryable}
    if pointer is not None:
        meta["pointer"] = pointer
    return {"content": [{"type": "text", "text": message}], "isError": True, "_meta": meta}
