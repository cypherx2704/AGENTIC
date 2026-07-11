"""FastAPI application factory + lifespan wiring.

Installs the trace middleware and Contract 2 exception handlers, mounts the memory +
session + GDPR + health routers, and manages the lifespan: open the DB pool, pick the
repository (Postgres when a pool opened, else the in-memory fallback so the service still
serves with degraded durability), wire the embeddings client (gateway + deterministic
mock fallback), the lazy Valkey client (soft dependency), the aiokafka outbox publisher,
the periodic TTL sweep, and warm JWKS — closing all on shutdown. Configures structlog at
import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time (before uvicorn creates the loop) fixes local
# Windows dev; no-op on Linux/macOS (prod), so it is safe to set unconditionally here.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import gdpr, health, memories, sessions
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .db import pool as db_pool
from .db.outbox import OutboxPublisher
from .db.valkey import ValkeyClient
from .services.embeddings import EmbeddingClient
from .services.pg_repository import PgMemoryRepository
from .services.repository import InMemoryRepository
from .services.service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


async def _ttl_sweep_loop(app: FastAPI) -> None:
    """Periodically hard-delete expired memories in bounded batches (lifespan job)."""
    settings = app.state.settings
    from .core import metrics

    while True:
        await asyncio.sleep(settings.ttl_sweep_interval_seconds)
        repo = getattr(app.state, "repo", None)
        if repo is None:
            continue
        try:
            swept = await repo.sweep_expired(batch_size=settings.ttl_sweep_batch_size)
            if swept:
                metrics.ttl_swept_total.inc(swept)
                logger.info("ttl_sweep", swept=swept)
        except Exception as exc:  # noqa: BLE001 — sweep must keep running
            logger.warning("ttl_sweep_failed", error=str(exc))


async def _consolidation_loop(app: FastAPI) -> None:
    """Periodically consolidate/forget low-importance old memories (opt-in; OFF default).

    Started ONLY when ``memory_consolidation_enabled`` is True, so by default this loop
    never exists and cannot affect behavior. Soft-deletes to the audit trail.
    """
    settings = app.state.settings
    from .services import consolidation

    while True:
        await asyncio.sleep(settings.memory_consolidation_interval_seconds)
        repo = getattr(app.state, "repo", None)
        if repo is None:
            continue
        try:
            forgotten = await consolidation.run_consolidation_once(
                repo,
                max_importance=settings.memory_consolidation_max_importance,
                min_age_seconds=settings.memory_consolidation_min_age_seconds,
                batch_size=settings.memory_consolidation_batch_size,
            )
            if forgotten:
                logger.info("consolidation_swept", forgotten=forgotten)
        except Exception as exc:  # noqa: BLE001 — consolidation must keep running
            logger.warning("consolidation_failed", error=str(exc))


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
    pool_ok = False
    try:
        await pool.open(wait=False)
        pool_ok = True
    except Exception as exc:  # noqa: BLE001 — DB may be down at boot; readyz reports it
        logger.warning("db_pool_open_failed", error=str(exc))

    # ── Repository: Postgres when a pool is available, else in-memory fallback ──
    if pool_ok:
        app.state.repo = PgMemoryRepository(
            pool, producer_version=settings.service_version, default_visibility="isolated",
            contradiction_enabled=settings.memory_contradiction_enabled,
            contradiction_sim_min=settings.memory_contradiction_sim_min,
            vector_quantization=settings.memory_vector_quantization,
            hnsw_ef_search=settings.memory_hnsw_ef_search,
        )
    else:
        logger.warning("memory_repo_degraded_in_memory")
        app.state.repo = InMemoryRepository(
            contradiction_enabled=settings.memory_contradiction_enabled,
            contradiction_sim_min=settings.memory_contradiction_sim_min,
        )

    # ── Valkey (lazy client; soft dependency — readyz reports, never gates) ─────
    # Created BEFORE the embedder so the B2 content-hash embedding cache can use it.
    valkey = ValkeyClient(settings.valkey_url, ping_timeout=settings.valkey_ping_timeout_seconds)
    app.state.valkey = valkey

    # ── Embeddings client (llms-gateway + deterministic mock fallback + B2 cache) ──
    # Service-token provider lets the embeddings call forward the caller's tenant identity
    # (Contract-12) so the gateway resolves that tenant's BYOK key.
    embed_tokens = ServiceTokenProvider(settings)
    app.state.embed_tokens = embed_tokens
    app.state.embedder = EmbeddingClient(settings, tokens=embed_tokens, valkey=valkey)

    # ── Outbox publisher (Kafka connect is lazy + fail-soft) ────────────────────
    publisher = OutboxPublisher(pool, settings.kafka_brokers)
    app.state.outbox_publisher = publisher
    await publisher.start()

    # ── TTL sweep loop ──────────────────────────────────────────────────────────
    sweep_task: asyncio.Task[None] | None = None
    if settings.ttl_sweep_enabled:
        sweep_task = asyncio.create_task(_ttl_sweep_loop(app), name="ttl-sweep")
    app.state.ttl_sweep_task = sweep_task

    # ── Consolidation / forgetting loop (opt-in; OFF by default -> never started) ──
    consolidation_task: asyncio.Task[None] | None = None
    if settings.memory_consolidation_enabled:
        consolidation_task = asyncio.create_task(
            _consolidation_loop(app), name="consolidation"
        )
    app.state.consolidation_task = consolidation_task

    # ── JWKS warm (best-effort) ─────────────────────────────────────────────────
    warm_jwks(settings)

    logger.info("startup_complete", environment=settings.environment, mock=settings.use_mock_embeddings)
    try:
        yield
    finally:
        if sweep_task is not None:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
        if consolidation_task is not None:
            consolidation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consolidation_task
        await publisher.stop()
        await app.state.embedder.close()
        await embed_tokens.aclose()
        await valkey.close()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX Memory Service",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(memories.router)
    app.include_router(sessions.router)
    app.include_router(gdpr.router)
    return app


app = create_app()
