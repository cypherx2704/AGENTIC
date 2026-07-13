"""Bootstrap the PUBLIC ``web_search`` flow-tool (Phase 5 · 5-websearch).

This is the drop-in replacement for the bespoke ``Tools/tool-web-search`` service, rebuilt as a
Node-RED flow-tool served through the SINGLETON platform (public) runtime. Given a running platform
Node-RED it:

1. **deploys** the packaged ``web_search`` flow (``assets/web_search_flow.json``) into a runtime
   (``Publisher.deploy_flow`` -> Admin API ``create_flow``);
2. **publishes** it as an atomic tool + auto-singleton MCP (``Publisher.create_tool``) — snake_name
   ``web_search``, input schema ``{query:string (required), count?:integer}``, output
   ``{results:[{title,url,snippet,rank}]}`` (the tool-web-search contract);
3. **promotes** that MCP to PUBLIC (``Publisher.promote_mcp``) — which ensures the singleton platform
   runtime, RE-HOMES the flow onto it (so ``runtime_id`` = the platform runtime and
   ``node_red_flow_id`` = the deployed flow), registers it under the platform (public) namespace via
   ``registry_client.register_platform`` (``visibility='public'``, ``tenant_id NULL``), and retires
   the old tenant server_name — so agents discover + invoke ``web_search``.

WHY publish-then-promote (not a direct empty-GUC platform insert): the ``flow_tools.tools`` /
``mcps`` / ``mcp_tools`` RLS *write* policies (migration 0004) require ``tenant_id = app.tenant_id``,
so a row can only be INSERTed inside a matching TENANT context — the empty-GUC platform context is
read-only for these tables. ``promote_mcp`` is therefore the sanctioned, fully-tested path to
``visibility='public'``. This reuses the existing publisher/provisioner/registry code paths verbatim.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import structlog

from ..core.auth import Principal
from .publisher import Publisher

logger = structlog.get_logger(__name__)

# ── the web_search tool contract (matches Tools/tool-web-search's manifest) ───────────
WEB_SEARCH_SNAKE_NAME = "web_search"
WEB_SEARCH_TITLE = "Web Search"
WEB_SEARCH_DESCRIPTION = "Search the web and return ranked results with snippets."

# Input schema {query:string (required), count?:integer 1..20}. `count` is the public param name
# (also Brave's native param); the flow ALSO accepts `max_results` as a drop-in alias so callers of
# the old tool-web-search still work. Rendered by manifest_builder.form_to_input_schema.
WEB_SEARCH_INPUT_PARAMS: list[dict[str, Any]] = [
    {"name": "query", "type": "string", "required": True, "description": "Search query."},
    {
        "name": "count",
        "type": "integer",
        "required": False,
        "minimum": 1,
        "maximum": 20,
        "description": "Maximum number of results to return (1..20, default 5).",
    },
]
# Output schema {results:[{title,url,snippet,rank}]} — the tool-web-search output shape.
WEB_SEARCH_OUTPUT_PARAMS: list[dict[str, Any]] = [
    {
        "name": "results",
        "type": "array",
        "items_type": "object",
        "description": "Ranked results: each {title, url, snippet, rank}.",
    },
]


def load_web_search_flow() -> dict[str, Any]:
    """Return the packaged web_search Node-RED flow (single-flow Admin-API object). Shipped in the
    wheel so the bootstrap can deploy it via the Admin API without a checkout of the chart."""
    raw = resources.files("tool_flow_bridge.assets").joinpath("web_search_flow.json").read_text(
        encoding="utf-8"
    )
    return json.loads(raw)


async def bootstrap_web_search(
    *,
    publisher: Publisher,
    principal: Principal,
    user_jwt: str,
    flow: dict[str, Any] | None = None,
    trace_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deploy + publish + promote the public ``web_search`` flow-tool. Idempotent-friendly: a
    re-run re-publishes (version bump) and re-promotes. Requires ``user_jwt`` to carry
    ``tool:admin`` + ``tenant:admin`` + ``platform:admin`` (promote is platform-only)."""
    flow = flow if flow is not None else load_web_search_flow()

    # 1. Deploy the flow into the tenant runtime so create_tool can read + validate its shape.
    flow_id, runtime = await publisher.deploy_flow(principal, flow)

    # 2. Publish as an atomic tool + auto-singleton MCP (tenant namespace, protected).
    created = await publisher.create_tool(
        principal,
        user_jwt,
        {
            "node_red_flow_id": flow_id,
            "title": WEB_SEARCH_TITLE,
            "description": WEB_SEARCH_DESCRIPTION,
            "snake_name": WEB_SEARCH_SNAKE_NAME,
            "input_params": WEB_SEARCH_INPUT_PARAMS,
            "output_params": WEB_SEARCH_OUTPUT_PARAMS,
            "access_mode": "automated",
            "visibility": "protected",
        },
        trace_headers=trace_headers,
    )
    if not created.get("mcps"):
        raise RuntimeError("web_search publish did not produce a singleton MCP to promote.")
    mcp_id = created["mcps"][0]["mcp_id"]

    # 3. Promote the singleton MCP to PUBLIC — re-homes the flow onto the platform runtime and
    #    registers it via registry.register_platform (the sole path to visibility='public').
    public_mcp = await publisher.promote_mcp(
        principal, user_jwt, mcp_id, trace_headers=trace_headers
    )

    logger.info(
        "web_search_bootstrapped",
        tenant_flow_id=flow_id,
        public_slug=public_mcp.get("slug"),
        public_server_name=public_mcp.get("server_name"),
    )
    return {
        "tool": created,
        "public_mcp": public_mcp,
        "tenant_flow_id": flow_id,
        "tenant_runtime": runtime.get("internal_host"),
    }


# ── one-shot operator CLI: python -m tool_flow_bridge.services.bootstrap ───────────────
async def _run_cli() -> dict[str, Any]:
    """Wire the real service dependencies (mirroring main.lifespan) and run the bootstrap once.

    Identity is read from settings/env: BOOTSTRAP_TENANT_ID, BOOTSTRAP_AGENT_ID, BOOTSTRAP_USER_JWT.
    The user JWT is forwarded to the Tool Registry (X-Forwarded-Agent-JWT) and MUST carry
    tool:admin + tenant:admin + platform:admin.
    """
    import httpx

    from ..core.config import get_settings
    from ..db import pool as db_pool
    from .nodered_admin import NoderedAdmin
    from .provisioner import get_platform_provisioner, get_provisioner
    from .registry_client import RegistryClient
    from .service_token import ServiceTokenProvider

    settings = get_settings()
    if not (settings.bootstrap_tenant_id and settings.bootstrap_agent_id and settings.bootstrap_user_jwt):
        raise SystemExit(
            "Set BOOTSTRAP_TENANT_ID, BOOTSTRAP_AGENT_ID and BOOTSTRAP_USER_JWT (the user JWT must "
            "carry tool:admin + tenant:admin + platform:admin) before running the bootstrap."
        )

    pool = db_pool.create_pool(
        settings.database_url, min_size=settings.db_pool_min_size, max_size=settings.db_pool_max_size
    )
    await pool.open(wait=True)
    http_client = httpx.AsyncClient()
    token_provider = ServiceTokenProvider(settings, client=http_client)
    try:
        registry = RegistryClient(settings, token_provider, http_client)
        publisher = Publisher(
            settings=settings,
            pool=pool,
            provisioner=get_provisioner(settings),
            registry=registry,
            nodered_admin=NoderedAdmin(http_client, settings),
            http_client=http_client,
            platform_provisioner=get_platform_provisioner(settings),
        )
        principal = Principal(
            tenant_id=settings.bootstrap_tenant_id,
            agent_id=settings.bootstrap_agent_id,
            scopes=["tool:invoke", "tool:admin", "tenant:admin", "platform:admin"],
            principal_type="agent",
        )
        return await bootstrap_web_search(
            publisher=publisher, principal=principal, user_jwt=settings.bootstrap_user_jwt
        )
    finally:
        await token_provider.aclose()
        await http_client.aclose()
        await pool.close()


def cli() -> None:
    import asyncio
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(_run_cli())
    public = result["public_mcp"]
    print(  # noqa: T201 — operator-facing CLI output
        "web_search bootstrapped as PUBLIC MCP "
        f"'{public.get('server_name')}' (slug {public.get('slug')}, "
        f"visibility={public.get('visibility')}); tool 'web_search' is now discoverable."
    )


if __name__ == "__main__":
    cli()
