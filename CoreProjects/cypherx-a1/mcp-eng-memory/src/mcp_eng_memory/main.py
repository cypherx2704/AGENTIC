"""FastAPI app factory + lifespan for the stateless mcp-eng-memory server.

Wires the trace middleware + Contract-2 handlers, mounts health/manifest/invoke routers,
constructs the backend proxy client, and warms JWKS. No DB, no Kafka — stateless by design.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import health, invoke, manifest
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .services.backend import BackendClient

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.backend = BackendClient(settings)
    warm_jwks(settings)
    logger.info("startup_complete", environment=settings.environment, backend=settings.cypherxa1_base_url)
    try:
        yield
    finally:
        await app.state.backend.aclose()
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(title="CypherX mcp-eng-memory", version=settings.service_version, lifespan=lifespan)
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(manifest.router)
    app.include_router(invoke.router)
    return app


app = create_app()
