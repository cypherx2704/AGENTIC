"""FastAPI application factory + lifespan wiring.

Installs the trace middleware + Contract 2 exception handlers, mounts the health / copilot
/ graph / connectors / webhooks routers, and manages the lifespan: open the DB pool, build
the shared service-token provider + downstream SharedCore clients, construct the retrieval
orchestrator + copilot + graph-query services, start the outbox publisher, warm JWKS —
closing all on shutdown. Mirrors the xAgent ax-1 app-factory shape.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import connectors, copilot, graph, health, webhooks
from .copilot.queries import GraphQueryService
from .copilot.service import CopilotService
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware, init_tracing, shutdown_tracing
from .db import pool as db_pool
from .db.outbox import OutboxPublisher
from .ingestion.pipeline import KbResolver
from .retrieval.orchestrator import RetrievalOrchestrator
from .services.guardrails_client import GuardrailsClient
from .services.llms_client import LlmsClient
from .services.memory_client import MemoryClient
from .services.rag_client import RagClient
from .services.service_token import ServiceTokenProvider
from .services.valkey import ValkeyClient

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    init_tracing(settings)

    pool = db_pool.create_pool(settings.database_url)
    app.state.db_pool = pool
    if settings.db_pool_open_at_startup:
        try:
            await pool.open(wait=False)
        except Exception as exc:  # noqa: BLE001 — DB may be down at boot; readyz reports it
            logger.warning("db_pool_open_failed", error=str(exc))

    # Service-token provider + downstream SharedCore clients (shared, connection-pooled).
    token_provider = ServiceTokenProvider(settings)
    app.state.token_provider = token_provider
    app.state.guardrails_client = GuardrailsClient(settings, token_provider)
    app.state.llms_client = LlmsClient(settings, token_provider)
    app.state.rag_client = RagClient(settings, token_provider)
    app.state.memory_client = MemoryClient(settings, token_provider)

    # Valkey (soft — revocation mirror only).
    app.state.valkey = ValkeyClient(settings.valkey_url)

    # Domain services.
    app.state.kb_resolver = KbResolver(settings, app.state.rag_client)
    app.state.orchestrator = RetrievalOrchestrator(settings, app.state.rag_client)
    app.state.graph_queries = GraphQueryService(pool)
    app.state.copilot = CopilotService(
        pool=pool,
        settings=settings,
        guardrails=app.state.guardrails_client,
        llms=app.state.llms_client,
        memory=app.state.memory_client,
        orchestrator=app.state.orchestrator,
    )

    publisher = OutboxPublisher(pool, settings.kafka_brokers)
    app.state.outbox_publisher = publisher
    if settings.outbox_publisher_enabled:
        await publisher.start()

    warm_jwks(settings)

    logger.info("startup_complete", environment=settings.environment)
    try:
        yield
    finally:
        await shutdown_tracing()
        await publisher.stop()
        await app.state.valkey.aclose()
        await app.state.guardrails_client.aclose()
        await app.state.llms_client.aclose()
        await app.state.rag_client.aclose()
        await app.state.memory_client.aclose()
        await token_provider.aclose()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX cypherx-a1 — Autonomous Engineering Memory",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(copilot.router)
    app.include_router(graph.router)
    app.include_router(connectors.router)
    app.include_router(webhooks.router)
    # Self-contained UI-1 console served same-origin at /ui (no CORS, no token-exchange
    # code; the browser sends the agent JWT it holds). Mounted only if the asset is present.
    ui_dir = Path(__file__).parent / "ui"
    if ui_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    return app


app = create_app()
