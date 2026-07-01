"""Per-principal quota + rate enforcement (Auth Contract-19 memory limits).

Four limits make up the Contract-19 ``memory`` block:

* ``memories_max``        — max number of stored memories per principal (resource cap).
* ``storage_bytes_max``   — max total content bytes per principal (resource cap).
* ``stores_per_min``      — store requests/min (rate cap, Valkey fixed-window).
* ``retrieves_per_min``   — search requests/min (rate cap, Valkey fixed-window).

Resolution order (cheapest first, all FAIL-OPEN):

1. JWT ``plan`` claim (PRIMARY, no network) -> a known tier's limits.
2. In-process TTL cache of ``plan -> MemoryLimits``.
3. DB ``memory.pricing`` row for the plan (source-of-record seeded by migration).
4. In-code fallback map.

FAIL-OPEN: on ANY failure to resolve limits (no claim, unknown plan, DB down) the
default permissive tier is used and the request proceeds — availability wins. The
resource caps (memories_max / storage_bytes_max) are checked against live COUNT/SUM in
the store path; the rate caps use a Valkey fixed-window counter that also fails open
when Valkey is unavailable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from ..core import metrics
from ..core.config import Settings, get_settings
from ..core.errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from ..core.auth import Principal

logger = structlog.get_logger(__name__)

KNOWN_PLANS = ("free", "pro", "enterprise")


@dataclass(frozen=True)
class MemoryLimits:
    """Effective Contract-19 memory limits for a plan tier."""

    plan: str
    memories_max: int
    storage_bytes_max: int
    stores_per_min: int
    retrieves_per_min: int


# In-code cold-start fallback — mirrors the migration `memory.pricing` seed.
_FALLBACK_LIMITS: dict[str, MemoryLimits] = {
    "free": MemoryLimits("free", 1_000, 10 * 1024 * 1024, 60, 120),
    "pro": MemoryLimits("pro", 100_000, 1024 * 1024 * 1024, 600, 1_200),
    "enterprise": MemoryLimits("enterprise", 10_000_000, 1024 * 1024 * 1024 * 1024, 10_000, 20_000),
}

# ── In-process TTL cache (plan -> (expires_at, MemoryLimits)) ────────────────────
_cache: dict[str, tuple[float, MemoryLimits]] = {}


def clear_cache() -> None:
    """Drop all cached plan->limits entries (test/admin hook)."""
    _cache.clear()


def _cache_get(plan: str) -> MemoryLimits | None:
    entry = _cache.get(plan)
    if entry is None:
        return None
    expires_at, limits = entry
    if time.monotonic() >= expires_at:
        _cache.pop(plan, None)
        return None
    return limits


def _cache_put(plan: str, limits: MemoryLimits, ttl_seconds: float) -> None:
    _cache[plan] = (time.monotonic() + ttl_seconds, limits)


def _normalize_plan(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    plan = raw.strip().lower()
    return plan if plan in KNOWN_PLANS else None


def _default_limits(settings: Settings) -> MemoryLimits:
    plan = (settings.default_plan or "free").strip().lower()
    return _FALLBACK_LIMITS.get(plan, _FALLBACK_LIMITS["free"])


async def _fetch_from_db(pool: AsyncConnectionPool, plan: str) -> MemoryLimits | None:
    """Read the ``memory.pricing`` row for ``plan`` (platform-scoped, no RLS)."""
    if pool is None:
        return None
    sql = """
        SELECT plan, memories_max, storage_bytes_max, stores_per_min, retrieves_per_min
          FROM memory.pricing
         WHERE plan = %s
    """
    try:
        async with pool.connection(timeout=2.0) as conn:
            cur = await conn.execute(sql, (plan,))
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — DB down: caller fails open
        logger.warning("memory_pricing_db_read_failed", plan=plan, error=str(exc))
        return None
    if row is None:
        return None
    return MemoryLimits(
        plan=str(row[0]),
        memories_max=int(row[1]),
        storage_bytes_max=int(row[2]),
        stores_per_min=int(row[3]),
        retrieves_per_min=int(row[4]),
    )


async def resolve_limits(
    principal: Principal,
    *,
    pool: AsyncConnectionPool | None = None,
    settings: Settings | None = None,
) -> MemoryLimits:
    """Resolve the effective :class:`MemoryLimits` for ``principal``'s plan. NEVER raises."""
    settings = settings or get_settings()
    ttl = settings.plan_cache_ttl_seconds

    plan = _normalize_plan(principal.raw_claims.get("plan"))
    if plan is None:
        metrics.quota_failopen_total.labels("no_claim").inc()
        return _default_limits(settings)

    cached = _cache_get(plan)
    if cached is not None:
        return cached

    limits = await _fetch_from_db(pool, plan) if pool is not None else None  # type: ignore[arg-type]
    if limits is None:
        limits = _FALLBACK_LIMITS.get(plan)
    if limits is None:
        metrics.quota_failopen_total.labels("unknown_plan").inc()
        return _default_limits(settings)

    _cache_put(plan, limits, ttl)
    return limits


# ── Rate enforcement (Valkey fixed window, fail-open) ────────────────────────────
async def enforce_rate(
    valkey: object,
    principal: Principal,
    *,
    dimension: str,
    limit: int,
    settings: Settings,
) -> None:
    """Enforce a per-principal per-minute rate cap. 429 over the limit; FAILS OPEN.

    ``dimension`` is 'stores_per_min' | 'retrieves_per_min'. With no Valkey, a slow
    Valkey, or a non-positive limit the check is skipped (allow) and a fail-open metric
    is bumped — availability wins over enforcement.
    """
    if not settings.quota_enabled or limit <= 0 or valkey is None:
        if valkey is None and settings.quota_enabled and limit > 0:
            metrics.quota_failopen_total.labels(dimension).inc()
        return

    ptype, pid = principal.memory_principal
    window = int(time.time()) // settings.quota_window_seconds
    key = f"{settings.quota_key_prefix}{principal.tenant_id}:{ptype}:{pid}:{dimension}:{window}"
    try:
        count = await valkey.incr_with_expire(  # type: ignore[attr-defined]
            key,
            ttl_seconds=settings.quota_window_seconds,
            timeout_seconds=settings.quota_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — Valkey down/slow: FAIL OPEN
        logger.warning("quota_rate_failopen", dimension=dimension, error=str(exc))
        metrics.quota_failopen_total.labels(dimension).inc()
        return

    if count > limit:
        metrics.quota_rejected_total.labels(dimension).inc()
        raise ApiError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            f"{dimension} limit of {limit} exceeded.",
            details={"reason": dimension.upper(), "limit": limit},
            headers={"Retry-After": str(settings.quota_window_seconds)},
        )


def enforce_resource_caps(
    *,
    limits: MemoryLimits,
    current_count: int,
    current_bytes: int,
    new_content_bytes: int,
) -> None:
    """Reject a store that would exceed the principal's resource caps (429 QUOTA_EXCEEDED).

    Pure + synchronous: the caller supplies the live COUNT/SUM (read inside the store
    txn). NOT fail-open — these are hard resource ceilings (the live numbers are only
    available when the DB is up; with no DB the caller skips this entirely, which IS the
    fail-open path).
    """
    if current_count >= limits.memories_max:
        metrics.quota_rejected_total.labels("memories_max").inc()
        raise ApiError(
            ErrorCode.QUOTA_EXCEEDED,
            f"memories_max limit of {limits.memories_max} reached.",
            details={"reason": "MEMORIES_MAX", "limit": limits.memories_max, "current": current_count},
        )
    if current_bytes + new_content_bytes > limits.storage_bytes_max:
        metrics.quota_rejected_total.labels("storage_bytes_max").inc()
        raise ApiError(
            ErrorCode.QUOTA_EXCEEDED,
            f"storage_bytes_max limit of {limits.storage_bytes_max} bytes would be exceeded.",
            details={
                "reason": "STORAGE_BYTES_MAX",
                "limit": limits.storage_bytes_max,
                "current": current_bytes,
            },
        )
