"""FastAPI application factory + lifespan wiring.

Installs the body-size guard + trace middleware and the Contract-2 exception handlers,
mounts the health / manifest / invoke routers, and manages a minimal lifespan: wire the
lazy Valkey client (soft dependency) and warm JWKS. This MCP server is stateless — no DB,
no Kafka, no config registries. Configures structlog at import time.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time is a no-op on Linux/macOS (prod) and keeps this
# service consistent with the rest of SharedCore, so it is safe to set unconditionally.
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import health, invoke, manifest
from .core.auth import warm_jwks
from .core.body_limit import BodySizeLimitMiddleware
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .core.valkey import ValkeyClient

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    # ── Valkey (lazy client; soft dependency — readyz reports, never gates) ────
    valkey = ValkeyClient(settings.valkey_url, ping_timeout=settings.valkey_ping_timeout_seconds)
    app.state.valkey = valkey

    # ── JWKS warm (best-effort) ─────────────────────────────────────────────────
    warm_jwks(settings)

    logger.info(
        "startup_complete",
        environment=settings.environment,
        search_provider=settings.search_provider,
    )
    try:
        yield
    finally:
        await valkey.close()
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="CypherX tool-web-search MCP server",
        version=settings.service_version,
        lifespan=lifespan,
    )
    # Middleware runs outermost-last-added-first. Add the body-size guard FIRST so it ends
    # up INSIDE the trace middleware: the trace contextvars (request_id/trace_id) are then
    # already bound when the 413 envelope is rendered.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(manifest.router)
    app.include_router(invoke.router)
    return app


app = create_app()
