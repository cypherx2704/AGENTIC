"""FastAPI application factory + lifespan wiring.

Installs the trace middleware + Contract 2 exception handlers, mounts the health + KB +
query + ingest + ACL routers, and manages the lifespan: open the DB pool, wire the lazy
Valkey client (soft dep), construct the embedder (service-token-backed, with mock fallback)
+ the object store, start the outbox publisher + the S3-deletion sweeper, start the
platform-skills bootstrap loop (lazy-with-retry — readiness gates only on the loop running),
warm JWKS — closing all on shutdown. Structlog is configured at import time.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time fixes local Windows dev; no-op on Linux/macOS.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import acls, health, ingest, kbs, query
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .db import pool as db_pool
from .db.outbox import OutboxPublisher
from .db.valkey import ValkeyClient
from .services.bootstrap import PlatformSkillsBootstrap
from .services.contextual import Contextualizer
from .services.decompose import QueryDecomposer
from .services.embeddings import EmbeddingClient
from .services.multiquery import QueryExpander
from .services.object_store import ObjectStore
from .services.rerank import RerankClient
from .services.service_token import ServiceTokenProvider
from .worker.sweeper import S3DeletionSweeper

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    # ── DB pool (best-effort open; readiness reflects actual connectivity) ─────
    pool = db_pool.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    app.state.db_pool = pool
    try:
        await pool.open(wait=False)
    except Exception as exc:  # noqa: BLE001 — DB may be down at boot; readyz reports it
        logger.warning("db_pool_open_failed", error=str(exc))

    # ── Valkey (lazy; soft dependency) ─────────────────────────────────────────
    valkey = ValkeyClient(settings.valkey_url, ping_timeout=settings.valkey_ping_timeout_seconds)
    app.state.valkey = valkey

    # ── Embedder (service-token-backed; mock fallback) + object store ──────────
    token_provider = ServiceTokenProvider(settings)
    app.state.token_provider = token_provider
    app.state.embedder = EmbeddingClient(settings, token_provider=token_provider)
    # Optional rerank client (no-op unless RAG_RERANK_ENABLED + a query opts in). Mock-tolerant.
    app.state.reranker = RerankClient(settings, token_provider=token_provider)
    # Optional contextual-ingest client (no-op unless RAG_CONTEXTUAL_INGEST). Mock-tolerant.
    app.state.contextualizer = Contextualizer(settings, token_provider=token_provider)
    # Optional query-transformation clients (no-op unless their flags + a per-query opt-in are on).
    app.state.decomposer = QueryDecomposer(settings, token_provider=token_provider)
    app.state.expander = QueryExpander(settings, token_provider=token_provider)
    app.state.object_store = ObjectStore(settings)

    # ── Outbox publisher (Kafka connect is lazy + fail-soft) ───────────────────
    publisher = OutboxPublisher(pool, settings.kafka_brokers)
    app.state.outbox_publisher = publisher
    await publisher.start()

    # ── S3-deletion sweeper ─────────────────────────────────────────────────────
    sweeper = S3DeletionSweeper(pool, app.state.object_store, settings)
    app.state.s3_sweeper = sweeper
    await sweeper.start()

    # ── Platform-skills bootstrap (lazy-with-retry; readyz gates on RUNNING) ───
    bootstrap = PlatformSkillsBootstrap(pool, settings)
    app.state.bootstrap = bootstrap
    await bootstrap.start()

    warm_jwks(settings)

    logger.info(
        "startup_complete", environment=settings.environment, mock_embeddings=settings.mock_embeddings
    )
    try:
        yield
    finally:
        await bootstrap.stop()
        await sweeper.stop()
        await publisher.stop()
        await app.state.embedder.aclose()
        await app.state.reranker.aclose()
        await app.state.contextualizer.aclose()
        await app.state.decomposer.aclose()
        await app.state.expander.aclose()
        await token_provider.aclose()
        await app.state.object_store.aclose()
        await valkey.close()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX RAG Service",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(kbs.router)
    app.include_router(query.router)
    app.include_router(ingest.router)
    app.include_router(acls.router)
    return app


app = create_app()
