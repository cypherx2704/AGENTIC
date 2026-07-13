"""Thin MCP server over GraphService — exposes the backend graph to AI coding
agents with **no LLM on the query path** (pure, deterministic index lookups).

When run via ``bkg-mcp`` it is backed by a :class:`Daemon` and refreshes the graph
before each query, so an agent always sees the current source (updated
incrementally). The tools mirror the CLI/GraphService surface exactly.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from .daemon import Daemon
from .service import GraphService


def build_server(service: GraphService, refresh: Callable[[], None] | None = None) -> FastMCP:
    server = FastMCP("bkg")

    def _fresh() -> None:
        if refresh is not None:
            refresh()

    @server.tool()
    def list_endpoints() -> list[dict[str, Any]]:
        """List every backend endpoint (method, resolved path, handler, confidence). No LLM."""
        _fresh()
        return service.list_endpoints()

    @server.tool()
    def get_endpoint(method: str, path: str) -> dict[str, Any] | None:
        """Get one endpoint by HTTP method and fully-resolved path (incl. body/response
        DTO refs, params, auth, confidence/partial). Deterministic; no LLM."""
        _fresh()
        return service.get_endpoint(method, path)

    @server.tool()
    def search_endpoints(query: str) -> list[dict[str, Any]]:
        """Search endpoints by method/path/handler/tag substring (empty query = all).
        Deterministic; no LLM."""
        _fresh()
        return service.search_endpoints(query)

    @server.tool()
    def filter_by_method(method: str) -> list[dict[str, Any]]:
        """Endpoints with the given HTTP method (case-insensitive exact). Deterministic; no LLM."""
        _fresh()
        return service.filter_by_method(method)

    @server.tool()
    def filter_by_tag(tag: str) -> list[dict[str, Any]]:
        """Endpoints carrying the given tag (case-insensitive exact). Deterministic; no LLM."""
        _fresh()
        return service.filter_by_tag(tag)

    @server.tool()
    def list_schemas() -> list[dict[str, Any]]:
        """List DTO/schema definitions (id, fields). Deterministic; no LLM."""
        _fresh()
        return service.list_schemas()

    @server.tool()
    def list_config() -> list[dict[str, Any]]:
        """List the configuration surface (env-var reads + BaseSettings fields). Deterministic; no LLM."""
        _fresh()
        return service.list_config()

    @server.tool()
    def blast_radius(schema_id: str) -> list[str]:
        """Endpoints affected if the DTO `schema_id` (file:Model) changes. Deterministic; no LLM."""
        _fresh()
        return service.blast_radius(schema_id)

    @server.tool()
    def trust_summary() -> dict[str, Any]:
        """Confidence/partial summary of the served facts (certain vs inferred). Deterministic; no LLM."""
        _fresh()
        return service.trust_summary()

    return server


def main() -> None:  # pragma: no cover - blocking server
    root = os.environ.get("BKG_PROJECT", ".")
    daemon = Daemon(root)
    build_server(daemon.service, refresh=daemon.resync).run()
