"""Agent-config Valkey read-through cache (Component 1, WP08).

The LOAD stage resolves an agent's runtime row on EVERY task. This module fronts that
read with a Valkey cache keyed by ``agent_id`` so the hot path skips a Postgres
round-trip on a hit. PUT ``/v1/agents/{id}/runtime`` invalidates the key on every config
change. TTL (default 5min) backstops a missed invalidation so a stale entry self-heals.

FAIL-OPEN by design (correctness over latency): a cache MISS, an ABSENT/disabled cache,
or ANY Valkey error all fall through to a LIVE DB read. A cache outage therefore never
fails a task — at worst the hot path is exactly as slow as the uncached path. Cross-
tenant safety is preserved because the underlying ``agents_repo.get_agent`` is still
RLS-scoped on the read-through, so the value cached under a key is whatever THAT tenant's
RLS-scoped read returned.

── The narrow Valkey interface this module uses (so the task-core agent can confirm it) ──
``services/valkey.py`` (the ``ValkeyClient``) is owned by the task-core agent; this module
does NOT edit it. It reads ``app.state.valkey`` via ``getattr`` and talks to the underlying
redis-asyncio client through the client's OWN public surface:

  * ``valkey.client()``            -> the live ``redis.asyncio.Redis`` (lazy; may raise)
  * ``redis.get(key)``             -> ``bytes | None``
  * ``redis.set(key, value, ex=)`` -> set with a TTL
  * ``redis.delete(key)``          -> delete (invalidate)

Only ``ValkeyClient.client()`` is relied upon (already public + used by ``ping()`` /
revocation). The conftest network-free ``_FakeValkey`` double has NO ``client()`` method,
so under test ``_raw_client()`` returns ``None`` and the cache transparently BYPASSES to a
DB read — the unit suite stays green with zero network. If the task-core agent's interface
ever stops exposing ``client()``, this module degrades to bypass rather than crashing.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

from ..core import metrics
from ..core.config import Settings
from ..db import agents_repo
from ..models.agent import AgentRuntime

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


def _raw_client(valkey: Any) -> Any | None:
    """Return the underlying redis-asyncio client, or ``None`` to BYPASS the cache.

    Narrow + defensive: a ``None`` ``app.state.valkey``, a double without ``client()``
    (the conftest fake), or any error building the lazy client all yield ``None`` so the
    caller falls through to a DB read. Never raises.
    """
    if valkey is None:
        return None
    factory = getattr(valkey, "client", None)
    if not callable(factory):
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 — building the lazy client must never fail LOAD
        logger.warning("agent_config_cache_client_unavailable", error=str(exc))
        return None


def _key(settings: Settings, agent_id: str) -> str:
    """Cache key for an agent's runtime config (``<prefix><agent_id>``)."""
    return f"{settings.agent_config_cache_key_prefix}{agent_id}"


async def get_runtime(
    valkey: Any,
    pool: AsyncConnectionPool,
    settings: Settings,
    tenant_id: str,
    agent_id: str,
) -> AgentRuntime | None:
    """Read-through: return the agent's runtime config, caching the DB result on a miss.

    Order of resolution:
      1. cache DISABLED / no Valkey -> straight RLS-scoped DB read (``bypass``);
      2. cache HIT -> deserialise + return (no DB touch);
      3. cache MISS / Valkey ERROR -> RLS-scoped DB read, then best-effort backfill.

    Returns ``None`` exactly when the agent has no runtime row (same contract as
    ``agents_repo.get_agent``) — a ``None`` is NOT cached (so a freshly-registered agent
    is visible immediately without waiting out a negative-cache TTL).
    """
    redis = _raw_client(valkey) if settings.agent_config_cache_enabled else None
    if redis is None:
        metrics.agent_config_cache_total.labels("bypass").inc()
        return await agents_repo.get_agent(pool, tenant_id, agent_id)

    key = _key(settings, agent_id)
    budget = settings.agent_config_cache_valkey_timeout_seconds

    # 1) Try the cache (fail-open on any error/timeout).
    try:
        async with asyncio.timeout(budget):
            raw = await redis.get(key)
        if raw is not None:
            cached = _deserialise(raw)
            if cached is not None and cached.tenant_id == tenant_id:
                metrics.agent_config_cache_total.labels("hit").inc()
                return cached
            # A key collision across tenants must never serve another tenant's config;
            # treat a tenant mismatch (or a corrupt blob) as a miss and re-read under RLS.
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN to a DB read
        metrics.agent_config_cache_total.labels("error").inc()
        logger.warning("agent_config_cache_get_failed", agent_id=agent_id, error=str(exc))
        return await agents_repo.get_agent(pool, tenant_id, agent_id)

    # 2) Miss -> RLS-scoped DB read, then best-effort backfill.
    metrics.agent_config_cache_total.labels("miss").inc()
    runtime = await agents_repo.get_agent(pool, tenant_id, agent_id)
    if runtime is not None:
        await _backfill(redis, key, runtime, settings)
    return runtime


async def _backfill(redis: Any, key: str, runtime: AgentRuntime, settings: Settings) -> None:
    """Store the resolved runtime config with the configured TTL (best-effort)."""
    try:
        async with asyncio.timeout(settings.agent_config_cache_valkey_timeout_seconds):
            await redis.set(
                key,
                runtime.model_dump_json(),
                ex=settings.agent_config_cache_ttl_seconds,
            )
    except Exception as exc:  # noqa: BLE001 — a failed backfill only costs the next read a DB hit
        logger.warning("agent_config_cache_backfill_failed", error=str(exc))


async def invalidate(valkey: Any, settings: Settings, agent_id: str) -> None:
    """Bust the cached runtime config for ``agent_id`` (called by PUT on every change).

    Best-effort + fail-soft: an absent Valkey or a delete error is benign because the TTL
    backstops a missed invalidation (a stale entry self-heals within the TTL window). The
    PUT response is authoritative regardless of whether the bust landed.
    """
    redis = _raw_client(valkey)
    if redis is None:
        return
    key = _key(settings, agent_id)
    try:
        async with asyncio.timeout(settings.agent_config_cache_valkey_timeout_seconds):
            await redis.delete(key)
        metrics.agent_config_cache_total.labels("invalidate").inc()
    except Exception as exc:  # noqa: BLE001 — TTL backstops a failed bust
        logger.warning("agent_config_cache_invalidate_failed", agent_id=agent_id, error=str(exc))


def _deserialise(raw: object) -> AgentRuntime | None:
    """Parse a cached JSON blob back into an ``AgentRuntime``; ``None`` if unparseable.

    A corrupt / schema-drifted blob (e.g. after a model change) is treated as a miss
    rather than an error so a bad cache entry can never wedge the LOAD stage.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        data = json.loads(str(raw))
        return AgentRuntime.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — corrupt cache entry -> miss (re-read from DB)
        logger.warning("agent_config_cache_deserialise_failed", error=str(exc))
        return None
