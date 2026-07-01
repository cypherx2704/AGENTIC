"""FastAPI application factory + lifespan wiring.

Installs the trace middleware and Contract 2 exception handlers, mounts the chat +
health routers, and manages the lifespan: open the DB pool, warm the DB-authoritative
config registries (pricing + aliases + capabilities) and keep them fresh with a
periodic refresh task, wire the lazy Valkey client (soft dependency), start the
aiokafka producer + outbox publisher task, warm JWKS — closing all on shutdown.
Configures structlog at import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time (before uvicorn creates the loop) fixes local
# Windows dev; it is a no-op on Linux/macOS (prod), so it is safe to set unconditionally here.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import chat, classify, embeddings, health, keys, read, rerank, rules
from .core import metrics
from .core.auth import warm_jwks
from .core.body_limit import BodySizeLimitMiddleware
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .db import pool as db_pool
from .db.outbox import OutboxPublisher
from .db.valkey import ValkeyClient
from .services.capabilities import capability_registry
from .services.cost import cost_calculator
from .services.router import ModelRouter

logger = structlog.get_logger(__name__)


async def _reload_registries(pool: object, model_router: ModelRouter) -> bool:
    """Reload aliases + pricing + capabilities from the DB into the in-process
    registries. Sets ``llms_config_source``: 'db' once ALL registries loaded from
    Postgres, 'fallback' while any is still on its cold-start fallback map.
    """
    pricing_ok = await cost_calculator.load_from_db(pool)  # type: ignore[arg-type]
    aliases_ok = await model_router.load_aliases()
    caps_ok = await capability_registry.load_from_db(pool)  # type: ignore[arg-type]
    from_db = pricing_ok and aliases_ok and caps_ok
    metrics.config_source.labels("db").set(1.0 if from_db else 0.0)
    metrics.config_source.labels("fallback").set(0.0 if from_db else 1.0)
    return from_db


async def _config_refresh_loop(app: FastAPI) -> None:
    """Periodically refresh the DB-authoritative config registries (Component 2), drive
    the billing-replay journal (WP05) so usage records that couldn't be written while the
    DB was down get re-driven once it recovers, and run the pricing-staleness watchdog
    (WP06). All steps are fail-open and best-effort."""
    from .services import billing_journal, pricing_staleness

    interval = app.state.settings.config_refresh_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await _reload_registries(app.state.db_pool, app.state.model_router)
        except Exception as exc:  # noqa: BLE001 — refresh must keep running
            logger.warning("config_refresh_failed", error=str(exc))
        try:
            await billing_journal.replay_pending(app.state.db_pool, settings=app.state.settings)
        except Exception as exc:  # noqa: BLE001 — replay is best-effort; loop must keep running
            logger.warning("billing_journal_replay_loop_failed", error=str(exc))
        try:
            # Pricing-staleness watchdog (WP06): WARN + optional webhook when the pricing
            # data is older than the configured max age. Production ALSO runs this from an
            # external scheduler — this in-process call is defense-in-depth. check_* never
            # raises, but guard the loop anyway.
            await pricing_staleness.check_pricing_staleness(
                app.state.db_pool, app.state.settings
            )
        except Exception as exc:  # noqa: BLE001 — watchdog must never take down the loop
            logger.warning("pricing_staleness_loop_failed", error=str(exc))


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

    # ── Config registries: pricing + aliases + capabilities (cold-start fallback
    # maps cover failures). Bounded so a slow/unreachable DB can never hang startup
    # (readyz reports DB state); a periodic task keeps the registries fresh.
    model_router = ModelRouter(settings, pool=pool)
    app.state.model_router = model_router
    metrics.config_source.labels("fallback").set(1.0)
    metrics.config_source.labels("db").set(0.0)
    try:
        await asyncio.wait_for(_reload_registries(pool, model_router), timeout=4.0)
    except Exception as exc:  # noqa: BLE001 — boot must not block on DB (incl. TimeoutError)
        logger.warning("db_warm_skipped", error=str(exc))
    refresh_task = asyncio.create_task(_config_refresh_loop(app), name="config-refresh")
    app.state.config_refresh_task = refresh_task

    # ── Valkey (lazy client; soft dependency — readyz reports, never gates) ────
    valkey = ValkeyClient(settings.valkey_url, ping_timeout=settings.valkey_ping_timeout_seconds)
    app.state.valkey = valkey

    # ── Outbox publisher (Kafka connect is lazy + fail-soft) ────────────────────
    publisher = OutboxPublisher(pool, settings.kafka_brokers)
    app.state.outbox_publisher = publisher
    await publisher.start()

    # ── JWKS warm (best-effort) ─────────────────────────────────────────────────
    warm_jwks(settings)

    logger.info("startup_complete", environment=settings.environment, mock=settings.mock_providers)
    try:
        yield
    finally:
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await publisher.stop()
        await valkey.close()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX LLMs Gateway",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    # Middleware runs outermost-last-added-first. Add the body-size guard FIRST so it
    # ends up INSIDE the trace middleware: the trace contextvars (request_id/trace_id)
    # are then already bound when the 413 envelope is rendered.
    settings = get_settings()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(embeddings.router)
    app.include_router(rerank.router)
    app.include_router(classify.router)
    app.include_router(read.router)
    app.include_router(keys.router)
    app.include_router(rules.router)
    return app


app = create_app()
