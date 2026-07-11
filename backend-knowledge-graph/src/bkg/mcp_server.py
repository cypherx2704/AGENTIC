"""Thin MCP server over GraphService — exposes the backend graph to AI coding
agents with **no LLM on the query path** (pure, deterministic index lookups).

The tools mirror the CLI/GraphService surface exactly; the transport is the only
difference. Run with ``bkg-mcp`` (loads ``$BKG_PROJECT`` or the CWD).
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .service import GraphService


def build_server(service: GraphService) -> FastMCP:
    server = FastMCP("bkg")

    @server.tool()
    def list_endpoints() -> list[dict[str, Any]]:
        """List every backend endpoint (method, resolved path, handler). Deterministic; no LLM."""
        return service.list_endpoints()

    @server.tool()
    def get_endpoint(method: str, path: str) -> dict[str, Any] | None:
        """Get one endpoint by HTTP method and fully-resolved path. Deterministic; no LLM."""
        return service.get_endpoint(method, path)

    return server


def main() -> None:
    root = os.environ.get("BKG_PROJECT", ".")
    build_server(GraphService.from_directory(root)).run()
