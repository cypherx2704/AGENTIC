"""The thin MCP server exposes the graph tools (no LLM on the query path)."""

from __future__ import annotations

import asyncio

from bkg.mcp_server import build_server
from bkg.service import GraphService


def test_tools_are_registered(fastapi_sources: dict[str, str]) -> None:
    server = build_server(GraphService.from_sources(fastapi_sources))
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "list_endpoints", "get_endpoint", "list_schemas", "list_config", "blast_radius",
        "trust_summary", "search_endpoints", "filter_by_method", "filter_by_tag",
    } <= names


def test_tool_call_refreshes_before_serving(fastapi_sources: dict[str, str]) -> None:
    calls = {"n": 0}

    def refresh() -> None:
        calls["n"] += 1

    server = build_server(GraphService.from_sources(fastapi_sources), refresh=refresh)
    asyncio.run(server.call_tool("list_endpoints", {}))
    assert calls["n"] == 1  # the graph is refreshed before the query is served
    asyncio.run(server.call_tool("search_endpoints", {"query": "user"}))
    assert calls["n"] == 2  # the new registry tools refresh too
