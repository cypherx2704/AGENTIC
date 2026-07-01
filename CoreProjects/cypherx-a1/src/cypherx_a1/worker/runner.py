"""Ingestion/extraction/consolidation worker entrypoint (``CYPHERXA1_RUN_WORKER=1``).

The horizontally-scalable async path. First cycle wires the **scheduled consolidation tick**
(Phase B, the user's "both" trigger): when ``CONSOLIDATION_SCHEDULE_ENABLED`` is set, the
worker periodically enumerates active tenants from the non-RLS ``outbox`` and runs the
reflection/consolidation pass per tenant so expertise summaries stay fresh without a manual
call. (The on-demand ``POST /v1/extract?consolidate=true`` remains the primary, agent-scoped
trigger.) The Kafka ingestion consumer over ``cypherx.cypherxa1.*`` is wired in Phase 1.5.
"""

from __future__ import annotations

import asyncio

import structlog

from ..core.config import get_settings
from ..core.logging import configure_logging
from ..db import pool as dbpool
from ..extraction.consolidator import run_consolidation
from ..extraction.expertise import run_expertise_refresh
from ..services.llms_client import LlmsClient
from ..services.service_token import ServiceTokenProvider

logger = structlog.get_logger(__name__)


async def _consolidation_tick(pool, llms: LlmsClient, settings) -> None:  # noqa: ANN001
    """Run consolidation for every tenant with recent activity (active tenants are those that
    have emitted outbox events). The outbox is non-RLS, so it can be scanned cross-tenant."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT partition_key FROM cypherx_a1.outbox")
        tenants = [r[0] for r in await cur.fetchall()]
    for tenant_id in tenants:
        try:
            stats = await run_consolidation(
                pool, tenant_id=tenant_id, agent_jwt="", agent_id=None, llms=llms, settings=settings
            )
            # Phase C: refresh recency-decayed Degree-of-Knowledge expert_in + ownership concentration.
            estats = await run_expertise_refresh(pool, tenant_id=tenant_id, settings=settings)
            logger.info("consolidation_tick", tenant_id=tenant_id,
                        summaries=stats.summaries_written, expert_edges=estats.expert_edges)
        except Exception as exc:  # noqa: BLE001 — one tenant must not abort the tick
            logger.warning("consolidation_tick_failed", tenant_id=tenant_id, error=str(exc))


async def run_worker() -> None:
    configure_logging()
    settings = get_settings()
    pool = dbpool.create_pool(settings.database_url)
    try:
        await pool.open(wait=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker_db_open_failed", error=str(exc))
    token_provider = ServiceTokenProvider(settings)
    llms = LlmsClient(settings, token_provider)

    logger.info(
        "worker_started",
        group=settings.ingestion_consumer_group,
        consolidation_schedule_enabled=settings.consolidation_schedule_enabled,
        note="Kafka ingestion consumer wired in Phase 1.5; consolidation tick active when enabled",
    )
    try:
        while True:
            if settings.consolidation_schedule_enabled:
                await _consolidation_tick(pool, llms, settings)
                await asyncio.sleep(settings.consolidation_interval_seconds)
            else:
                await asyncio.sleep(30)
                logger.info("worker_heartbeat")
    except asyncio.CancelledError:
        logger.info("worker_stopped")
        raise
    finally:
        await llms.aclose()
        await token_provider.aclose()
        try:
            await pool.close()
        except Exception:  # noqa: BLE001
            pass
