"""The thin MCP server exposes the graph tools (no LLM on the query path)."""

from __future__ import annotations

import asyncio

from bkg.mcp_server import build_server
from bkg.service import GraphService


def test_tools_are_registered(fastapi_sources: dict[str, str]) -> None:
    server = build_server(GraphService.from_sources(fastapi_sources))
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {"list_endpoints", "get_endpoint"} <= names
