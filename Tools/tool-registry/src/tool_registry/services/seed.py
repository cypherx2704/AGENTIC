"""Platform seed — register ``tool-web-search`` as a platform tool at startup.

Builds a Contract-4 manifest for the platform web-search tool from config (its base
URL is ``settings.tool_web_search_base_url`` — NEVER hardcoded) and idempotently
upserts the platform tool + version + capability/scope rows via
:func:`tool_registry.db.queries.seed_platform_tool`. Fail-soft: a seed failure logs a
warning and never blocks startup (readyz reports DB state separately).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ..core.config import Settings
from ..db import queries
from . import manifest as manifest_svc

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)

WEB_SEARCH_NAME = "tool-web-search"
WEB_SEARCH_VERSION = "1.0.0"


def build_web_search_manifest(settings: Settings) -> dict[str, Any]:
    """Construct the Contract-4 manifest for the platform web-search tool."""
    return {
        "schema_version": "1.0.0",
        "protocol_version": "mcp/1.0",
        "name": WEB_SEARCH_NAME,
        "display_name": "Web Search",
        "version": WEB_SEARCH_VERSION,
        "description": "Search the web and return ranked results with snippets.",
        "author": "CypherX Platform",
        "category": "research",
        "tags": ["search", "web", "information"],
        "auth_required": True,
        "required_scopes": ["tool:invoke", f"tool:{WEB_SEARCH_NAME}:invoke"],
        # base_url lets discovery resolve the invoke URL without a separate lookup.
        "base_url": settings.tool_web_search_base_url,
        "tools": [
            {
                "name": "web_search",
                "description": "Perform a web search and return top results.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "max_results": {"type": "integer", "default": 5, "maximum": 20},
                    },
                    "required": ["query"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "url": {"type": "string"},
                                    "snippet": {"type": "string"},
                                    "rank": {"type": "integer"},
                                },
                            },
                        }
                    },
                },
                "timeout_seconds": 30,
                "idempotent": True,
                "estimated_cost_usd": 0.001,
                "rate_limit": {"rpm": 60, "rpd": 5000},
            }
        ],
        "health_endpoint": "/livez",
        "metrics_endpoint": "/metrics",
    }


def seed_capabilities(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """(capability, required_scope) rows for the seed: one per declared tool."""
    server_name = manifest["name"]
    fine_scope = f"tool:{server_name}:invoke"
    return [(cap, fine_scope) for cap in manifest_svc.declared_capabilities(manifest)]


async def seed_platform_tools(pool: AsyncConnectionPool, settings: Settings) -> None:
    """Seed all platform tools (currently just tool-web-search). Fail-soft."""
    manifest = build_web_search_manifest(settings)
    try:
        manifest_svc.validate_manifest(manifest)
        tool_id = await queries.seed_platform_tool(
            pool,
            name=WEB_SEARCH_NAME,
            version=WEB_SEARCH_VERSION,
            manifest=manifest,
            capabilities=seed_capabilities(manifest),
        )
        logger.info("platform_tool_seeded", name=WEB_SEARCH_NAME, tool_id=tool_id)
    except Exception as exc:  # noqa: BLE001 — seeding is best-effort at boot
        logger.warning("platform_seed_failed", name=WEB_SEARCH_NAME, error=str(exc))
