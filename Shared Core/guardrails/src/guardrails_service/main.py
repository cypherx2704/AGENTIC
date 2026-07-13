"""FastAPI application factory + lifespan wiring.

Installs the trace middleware and Contract 2 exception handlers, mounts the check +
policies + health routers, and manages the lifespan: open the DB pool, build the
classifier + policy engine + redaction resolver, load the rules-registry metadata
overlay (then refresh it on a loop), wire the lazy Valkey client, start the aiokafka
producer + outbox publisher task, warm JWKS — closing all on shutdown. Configures
structlog at import time.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# psycopg3 async cannot run on Windows' default ProactorEventLoop. Selecting the
# SelectorEventLoop policy at import time (before uvicorn creates the loop) fixes local
# Windows dev; it is a no-op on Linux/macOS (prod), so it is safe to set unconditionally.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import FastAPI

from .api import check, health, policies, redaction_keys, rules, violations
from .core.auth import warm_jwks
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging
from .core.normalization import build_confusables_map
from .core.redaction import RedactionKeyResolver
from .core.trace import TraceContextMiddleware
from .core.valkey import ValkeyClient
from .db import pool as db_pool
from .db.maintenance import OutboxPurger
from .db.outbox import OutboxPublisher
from .db.persist_queue import PersistenceQueue
from .services.classifier import build_classifier, warm_classifier
from .services.pii_presidio import build_presidio_analyzer
from .services.policy_cache import PolicyCache, RateLimiter
from .services.policy_engine import PolicyEngine
from .services.redaction_keys import RedactionKeyRetirementJob
from .services.rules import CustomRuleLoader, RuleRegistryOverlay
from .services.startup_checks import assert_supported_stream_modes

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

    # ── Classifier (stub default; detoxify loads its PINNED checkpoint EAGERLY here,
    #    with graceful stub fallback when the dep/model is unavailable) ────────────
    app.state.classifier = build_classifier(settings)
    warm_classifier(app.state.classifier)

    # ── Optional Microsoft Presidio PII analyzer (flag GUARDRAILS_PII_PRESIDIO; default off).
    #    None when disabled/unavailable -> the check path runs the current regex/HMAC path. ──
    app.state.presidio_analyzer = build_presidio_analyzer(settings)

    # ── Unicode confusables skeleton map (B1 Layer C) — built ONCE from the checked-in
    #    Unicode data file. Consumed by the canonicalization detection view only when
    #    GUARDRAILS_CONFUSABLES_FOLD is on; empty map = a no-op fold (fail-soft). ──
    app.state.confusables_map = build_confusables_map()

    # ── Policy engine + DB-backed redaction key resolver (env fallback, cached) ──
    app.state.policy_engine = PolicyEngine(pool)
    app.state.redaction_resolver = RedactionKeyResolver(
        settings.redaction_hmac_key_platform,
        pool=pool,
        cache_ttl_seconds=settings.redaction_key_cache_ttl_seconds,
    )

    # ── Rules-registry metadata overlay (WP02 — DB authoritative, 60s refresh) ──
    rule_registry = RuleRegistryOverlay(
        pool, refresh_interval_seconds=settings.rules_refresh_interval_seconds
    )
    app.state.rule_registry = rule_registry
    await rule_registry.load_once()  # fail-soft; mismatch is surfaced via /readyz
    await rule_registry.start()

    # ── Custom-rule loader (WP07 — per-tenant DB rules -> live RULES_BY_ID) ──────
    # The check path calls loader.with_custom_rules(policy, tenant_id) (the one-line hook)
    # to register + enable a tenant's custom rules through the unmodified pipeline.
    app.state.custom_rule_loader = CustomRuleLoader(
        pool, ttl_seconds=settings.custom_rules_cache_ttl_seconds
    )

    # ── Valkey (lazy client; soft dependency — WP02 foundation; WP07 cache/limit) ──
    valkey = ValkeyClient(
        settings.valkey_url, timeout_seconds=settings.valkey_timeout_seconds
    )
    app.state.valkey = valkey

    # ── Hot-path policy cache (FAIL-OPEN) + per-tenant rate limiter (FAIL-CLOSED) ──
    # Both reference the Valkey client. The cache is purely a latency optimisation; the
    # limiter is DISABLED unless RATE_LIMIT_ENABLED is set, so unit/local stay green.
    app.state.policy_cache = PolicyCache(valkey, settings)
    app.state.rate_limiter = RateLimiter(valkey, settings)

    # ── Stream-mode fail-fast: refuse to boot if an active policy/rule needs an ──
    #    unsupported stream mode (a confirmed unsupported mode raises; a DB blip is tolerated).
    await assert_supported_stream_modes(pool)

    # ── Post-response persistence queue (drains violation/usage writes off the hot path) ──
    persist_queue = PersistenceQueue(
        # Dynamic getter so a pool opened after startup — or swapped in by a test — is seen
        # at drain time (mirrors the old inline path reading app.state.db_pool per request).
        lambda: getattr(app.state, "db_pool", None),
        producer_version=settings.service_version,
        maxsize=settings.persist_queue_maxsize,
        drain_timeout_seconds=settings.persist_queue_drain_timeout_seconds,
    )
    app.state.persist_queue = persist_queue
    await persist_queue.start()

    # ── Outbox publisher (Kafka connect is lazy + fail-soft) ────────────────────
    publisher = OutboxPublisher(pool, settings.kafka_brokers)
    app.state.outbox_publisher = publisher
    await publisher.start()

    # ── Outbox purge (Ops): retire PUBLISHED outbox rows past the retention window ──
    outbox_purger = OutboxPurger(
        pool,
        retention_hours=settings.outbox_retention_hours,
        interval_seconds=settings.outbox_purge_interval_seconds,
        enabled=settings.outbox_purge_enabled,
    )
    app.state.outbox_purger = outbox_purger
    await outbox_purger.start()

    # ── Redaction-key retirement (retire keys past the 30-day grace window) ──────
    key_retirement = RedactionKeyRetirementJob(
        pool,
        grace_days=settings.redaction_key_grace_days,
        interval_seconds=settings.redaction_key_retire_interval_seconds,
    )
    app.state.redaction_key_retirement = key_retirement
    await key_retirement.start()

    # ── JWKS warm (best-effort) ─────────────────────────────────────────────────
    warm_jwks(settings)

    logger.info(
        "startup_complete",
        environment=settings.environment,
        classifier_mode=settings.classifier_mode,
        rate_limit_enabled=settings.rate_limit_enabled,
        policy_cache_ttl_seconds=settings.policy_cache_ttl_seconds,
    )
    try:
        yield
    finally:
        # Drain the persistence queue FIRST so buffered audit/usage writes flush before
        # the pool closes; then stop the background jobs.
        await persist_queue.stop()
        await key_retirement.stop()
        await outbox_purger.stop()
        await publisher.stop()
        await rule_registry.stop()
        await app.state.valkey.aclose()
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("db_pool_close_failed", error=str(exc))
        logger.info("shutdown_complete")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="CypherX Guardrails Service",
        version=get_settings().service_version,
        lifespan=lifespan,
    )
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(check.router)
    app.include_router(policies.router)
    app.include_router(rules.router)
    app.include_router(violations.router)
    app.include_router(redaction_keys.router)
    return app


app = create_app()
