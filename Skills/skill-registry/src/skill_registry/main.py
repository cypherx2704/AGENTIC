"""FastAPI application factory + lifespan wiring.

Installs the trace middleware and Contract 2 exception handlers, mounts the health +
skills routers, and manages the lifespan: open the DB pool, wire the lazy Valkey
client (soft dependency) + a shared httpx client for manifest polling, seed the
platform skills, warm JWKS, and run the 30s manifest-health background sweep —
closing all on shutdown. Configures structlog at import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time (before uvicorn creates the loop) fixes local
# Windows dev; it is a no-op on Linux/macOS (prod), so it is safe to set unconditionally.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import structlog
from fastapi import FastAPI

from .api import health, skills
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.trace import TraceContextMiddleware
from .db import pool as db_pool
from .db.valkey import ValkeyClient
from .services import seed
from .services.health_runner import health_poll_loop

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    # ── DB pool (best-effort open; readiness reflects actual connectivity) ─────
    pool = db_pool.create_pool(settings.database_url)
    app.state.db_pool = pool
    try:
        await pool.open(wait=False)
    except Exception as exc:  # noqa: BLE001 — DB may be down at boot; readyz reports it
        logger.warning("db_pool_open_failed", error=str(exc))

    # ── Valkey (lazy client; soft dependency — readyz reports, never gates) ────
    valkey = ValkeyClient(settings.valkey_url, ping_timeout=settings.valkey_ping_timeout_seconds)
    app.state.valkey = valkey

    # ── Shared HTTP client for manifest polling ─────────────────────────────────
    http_client = httpx.AsyncClient(timeout=settings.health_poll_timeout_seconds)
    app.state.http_client = http_client

    # ── Platform seed (idempotent, fail-soft) ───────────────────────────────────
    if settings.seed_platform_skills:
        try:
            await asyncio.wait_for(seed.seed_platform_skills(pool, settings), timeout=4.0)
        except Exception as exc:  # noqa: BLE001 — boot must not block on DB
            logger.warning("seed_skipped", error=str(exc))

    # ── JWKS warm (best-effort) ─────────────────────────────────────────────────
    warm_jwks(settings)

    # ── Manifest-health background sweep (30s lifespan job) ─────────────────────
    health_task = asyncio.create_task(
        health_poll_loop(pool, http_client, settings), name="health-poll"
    )
    app.state.health_task = health_task

    logger.info("startup_complete", environment=settings.environment)
    try:
        yield
    finally:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        await http_client.aclose()
        await valkey.close()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX Skill Registry",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(skills.router)
    return app


app = create_app()
