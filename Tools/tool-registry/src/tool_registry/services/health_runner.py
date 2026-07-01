"""Health-poll orchestration — eager poll, sweep, and the lifespan loop (WP11).

Glues the pure state machine (``health_poll.next_health_state``) and the fail-soft
manifest GET (``health_poll.poll_manifest``) to DB persistence
(``db.queries.update_health``):

* :func:`poll_one` polls a single tool and persists the resulting health state. Used
  both for the EAGER poll at registration and inside the sweep.
* :func:`sweep_once` polls every registered tool once (platform-scoped read).
* :func:`health_poll_loop` is the 30s lifespan background job.

The HTTP client is injected (httpx.AsyncClient in prod, a fake in tests), so the
whole orchestration is exercisable without a network.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from ..core import metrics
from ..core.config import Settings
from ..db import queries
from .discovery import resolve_invoke_url
from .health_poll import (
    HealthState,
    HealthThresholds,
    HttpClient,
    next_health_state,
    poll_manifest,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


def _thresholds(settings: Settings) -> HealthThresholds:
    return HealthThresholds(
        degrade_after=settings.health_degrade_after,
        offline_after=settings.health_offline_after,
    )


async def poll_one(
    pool: AsyncConnectionPool,
    client: HttpClient,
    settings: Settings,
    *,
    tool_id: str,
    base_url: str,
    current: HealthState,
) -> HealthState:
    """Poll one tool's manifest, fold the outcome through the state machine, persist it.

    Returns the new :class:`HealthState`. Fail-soft throughout — a poll error advances
    the failure counter rather than raising.
    """
    result = await poll_manifest(
        client, base_url, last_etag=current.last_etag, timeout=settings.health_poll_timeout_seconds
    )
    new_state = next_health_state(
        current,
        success=result.success,
        new_etag=result.etag,
        thresholds=_thresholds(settings),
    )

    if result.success and not result.changed:
        metrics.health_poll_total.labels("unchanged").inc()
    elif result.success:
        metrics.health_poll_total.labels("ok").inc()
    else:
        metrics.health_poll_total.labels("error").inc()
    if new_state.status != current.status:
        metrics.health_transitions_total.labels(new_state.status).inc()
        logger.info(
            "tool_health_transition",
            tool_id=tool_id,
            from_status=current.status,
            to_status=new_state.status,
            failures=new_state.consecutive_failures,
        )

    await queries.update_health(
        pool,
        tool_id=tool_id,
        status=new_state.status,
        consecutive_failures=new_state.consecutive_failures,
        last_etag=new_state.last_etag,
        manifest=result.manifest if result.changed else None,
    )
    return new_state


def _current_state(row: dict[str, Any]) -> HealthState:
    """Build the current HealthState from a pollable-tools row (health may be absent)."""
    return HealthState(
        status=row.get("status") or "active",
        consecutive_failures=int(row.get("consecutive_failures") or 0),
        last_etag=row.get("last_etag"),
    )


async def sweep_once(
    pool: AsyncConnectionPool, client: HttpClient, settings: Settings
) -> int:
    """Poll every registered tool once. Returns the number of tools polled."""
    rows = await queries.list_pollable_tools(pool)
    counts: dict[str, int] = {}
    for row in rows:
        manifest = row.get("manifest")
        base_url = resolve_invoke_url(manifest, row["name"])
        current = HealthState(
            status="active",
            consecutive_failures=0,
            last_etag=row.get("last_etag"),
        )
        new_state = await poll_one(
            pool, client, settings,
            tool_id=str(row["tool_id"]), base_url=base_url, current=current,
        )
        counts[new_state.status] = counts.get(new_state.status, 0) + 1
    for status in ("active", "degraded", "offline"):
        metrics.tools_by_status.labels(status).set(counts.get(status, 0))
    return len(rows)


async def health_poll_loop(pool: AsyncConnectionPool, client: HttpClient, settings: Settings) -> None:
    """Lifespan background job: sweep all tools every ``health_poll_interval_seconds``."""
    interval = settings.health_poll_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            polled = await sweep_once(pool, client, settings)
            logger.info("health_sweep_complete", tools_polled=polled)
        except Exception as exc:  # noqa: BLE001 — the loop must keep running
            logger.warning("health_sweep_failed", error=str(exc))
