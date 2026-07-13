"""FastAPI application factory + lifespan wiring.

Installs the body-size guard + trace middleware and the Contract-2 exception handlers,
mounts the health / manifest / invoke / flow-tools / editor routers, and wires the
lifespan: open the psycopg3 pool (fail-soft), the lazy Valkey client (soft), a shared httpx
client, the Contract-12 service-token provider, the Tool Registry client, the Node-RED
admin client, the tenant-runtime provisioner, and the publish orchestrator.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import structlog
from fastapi import FastAPI

from .api import editor_sessions, flow_tools, health, manifest, mcp, tools_mcps
from .core.auth import warm_jwks
from .core.body_limit import BodySizeLimitMiddleware
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .core.valkey import ValkeyClient
from .db import pool as db_pool
from .services.nodered_admin import NoderedAdmin
from .services.provisioner import get_platform_provisioner, get_provisioner
from .services.publisher import Publisher
from .services.registry_client import RegistryClient
from .services.service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    # ── Postgres pool (hard dependency; fail-soft boot — readyz reflects it) ────
    pool = db_pool.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    try:
        await pool.open(wait=False)
    except Exception as exc:  # noqa: BLE001 — boot even if Postgres is down
        logger.warning("db_pool_open_deferred", error=str(exc))
    app.state.db_pool = pool

    # ── Valkey (soft) ────────────────────────────────────────────────────────
    valkey = ValkeyClient(settings.valkey_url, ping_timeout=settings.valkey_ping_timeout_seconds)
    app.state.valkey = valkey

    # ── Shared httpx client + downstream service clients ────────────────────────
    http_client = httpx.AsyncClient()
    app.state.http_client = http_client
    token_provider = ServiceTokenProvider(settings, client=http_client)
    app.state.token_provider = token_provider
    registry = RegistryClient(settings, token_provider, http_client)
    app.state.registry = registry
    nodered_admin = NoderedAdmin(http_client, settings)
    provisioner = get_provisioner(settings)
    platform_provisioner = get_platform_provisioner(settings)
    app.state.provisioner = provisioner
    app.state.platform_provisioner = platform_provisioner
    app.state.publisher = Publisher(
        settings=settings,
        pool=pool,
        provisioner=provisioner,
        registry=registry,
        nodered_admin=nodered_admin,
        http_client=http_client,
        platform_provisioner=platform_provisioner,
    )

    warm_jwks(settings)

    logger.info(
        "startup_complete",
        environment=settings.environment,
        provisioner_mode=settings.provisioner_mode,
        registry=settings.tool_registry_url,
    )
    try:
        yield
    finally:
        await valkey.close()
        await token_provider.aclose()
        await http_client.aclose()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="CypherX tool-flow-bridge",
        version=settings.service_version,
        lifespan=lifespan,
    )
    # Body-size guard added FIRST so it ends up INSIDE the trace middleware (trace
    # contextvars are bound when the 413 envelope is rendered).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(manifest.router)
    app.include_router(mcp.router)  # real-MCP Streamable-HTTP (JSON-RPC 2.0) — the sole tool wire
    app.include_router(flow_tools.router)
    app.include_router(tools_mcps.tools_router)  # POST/GET /v1/tools
    app.include_router(tools_mcps.mcps_router)  # /v1/mcps CRUD + publish/promote
    app.include_router(editor_sessions.router)
    return app


app = create_app()
