"""Pricing-staleness watchdog (WP06).

Provider pricing lives in ``llms.provider_pricing`` and is updated out-of-band (PR-managed
migrations / an ops job). If that data goes stale, every ``cost_usd`` the gateway computes
silently drifts from reality. This module checks the AGE of the pricing data (the most
recent ``updated_at`` across the table) against a configured max age and, when stale,
logs a WARN and (optionally) POSTs an alert to a webhook sink.

Wiring:

* ``check_pricing_staleness(pool, settings)`` is called best-effort from the lifespan
  config-refresh loop (``main._config_refresh_loop``) on the same cadence as the registry
  refresh. It NEVER raises — a watchdog must not take the loop down.
* PRODUCTION also runs an equivalent check from an external scheduler (a CronJob / the
  platform scheduler) so staleness is caught even if a single gateway pod's refresh loop
  is wedged. The in-process call here is defense-in-depth, not the sole alarm.

Sink: ``settings.pricing_staleness_max_age_seconds`` (default 7 days) is the threshold;
``settings.pricing_staleness_webhook_url`` (default empty) is the alert target — when
empty the check is LOG-ONLY (no webhook). A webhook failure is itself logged and
swallowed (best-effort).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from ..core import metrics

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from ..core.config import Settings

logger = structlog.get_logger(__name__)


async def _post_webhook(url: str, payload: dict[str, object], *, timeout_seconds: float) -> None:
    """Best-effort POST of the staleness alert to the configured sink. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        logger.warning("pricing_staleness_webhook_failed", error=str(exc))


async def check_pricing_staleness(
    pool: AsyncConnectionPool | None,
    settings: Settings,
) -> bool:
    """Check the age of the pricing data; WARN + (optionally) alert when stale. NEVER raises.

    Returns True when the pricing data was found to be STALE (older than the configured
    max age), False otherwise (fresh, no pool, or the age could not be determined).
    """
    if pool is None:
        metrics.pricing_staleness_seconds.set(-1.0)
        return False

    try:
        from ..db.pool import fetch_pricing_max_updated_at

        latest = await fetch_pricing_max_updated_at(pool)
    except Exception as exc:  # noqa: BLE001 — watchdog must never take down the loop
        logger.warning("pricing_staleness_check_failed", error=str(exc))
        metrics.pricing_staleness_seconds.set(-1.0)
        return False

    if latest is None:
        # Empty pricing table — surface it the same as "could not determine".
        logger.warning("pricing_staleness_unknown", reason="no_pricing_rows")
        metrics.pricing_staleness_seconds.set(-1.0)
        return False

    now = datetime.now(UTC)
    # Normalize a possibly-naive timestamp to UTC so the subtraction is well-defined.
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    age_seconds = (now - latest).total_seconds()
    metrics.pricing_staleness_seconds.set(age_seconds)

    max_age = settings.pricing_staleness_max_age_seconds
    if age_seconds <= max_age:
        logger.debug("pricing_fresh", age_seconds=age_seconds, max_age_seconds=max_age)
        return False

    logger.warning(
        "pricing_stale",
        age_seconds=age_seconds,
        max_age_seconds=max_age,
        last_updated_at=latest.isoformat(),
    )

    webhook = settings.pricing_staleness_webhook_url
    if webhook:
        await _post_webhook(
            webhook,
            {
                "alert": "pricing_stale",
                "service": settings.service_name,
                "environment": settings.environment,
                "age_seconds": age_seconds,
                "max_age_seconds": max_age,
                "last_updated_at": latest.isoformat(),
            },
            timeout_seconds=settings.pricing_staleness_webhook_timeout_seconds,
        )
    return True
