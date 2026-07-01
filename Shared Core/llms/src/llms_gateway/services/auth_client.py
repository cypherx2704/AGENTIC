"""Plan-tier + effective-limits resolver (WP05).

Resolves a tenant's **plan tier** (free | pro | enterprise) and the effective
rate/quota limits for that tier, for the rate limiter to enforce.

Resolution order (cheapest first, all fail-open):

1. **JWT ``plan`` claim** (PRIMARY, no network) — Auth WP03 stamps the tenant plan
   onto the agent JWT, surfaced as ``principal.raw_claims["plan"]``.
2. **In-process TTL cache** of ``plan -> PlanLimits`` (default 60s, env-overridable
   via ``plan_cache_ttl_seconds``) — avoids a DB hit per request.
3. **DB ``llms.rate_limits`` row** for the resolved plan (the source-of-record that
   mirrors the Auth ``plan_defaults`` ``llms`` block; seeded by migration 0004).
4. **Auth HTTP ``GET {auth_base_url}/v1/tenants/{id}/limits``** — see ``_fetch_from_auth``.

FAIL-OPEN: on ANY error (no claim, unknown plan, DB down, Auth down) the resolver
returns the **default permissive tier** (``settings.default_plan``, falling back to
the built-in ``_FALLBACK_LIMITS`` if even that is unknown), logs, and bumps
``plan_resolve_failopen_total``. The rate limiter must always get *some* limits.

ASSUMPTION / TODO (documented per WP05 brief): the codebase has **no service-token
provider** today (auth.py only verifies inbound tokens; nothing mints an outbound
service JWT, and there is no shared httpx downstream-call helper). So
``_fetch_from_auth`` is a TODO STUB that is never invoked by the default path; the
HTTP fallback can be enabled once a service-token provider lands. Until then the
effective limits come from the JWT plan claim + the DB ``rate_limits`` row.

Public API (call these from the chat path):

    limits = await resolve_limits(principal, pool=app.state.db_pool, settings=app.state.settings)
        -> PlanLimits   # never raises; fail-open returns the default tier

``PlanLimits`` is the dataclass the rate limiter consumes (see ``services/rate_limit.py``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from ..core import metrics
from ..core.config import Settings, get_settings

if TYPE_CHECKING:  # avoid importing heavy/optional deps at module import time
    from psycopg_pool import AsyncConnectionPool

    from ..core.auth import Principal

logger = structlog.get_logger(__name__)

KNOWN_PLANS = ("free", "pro", "enterprise")


@dataclass(frozen=True)
class PlanLimits:
    """Effective per-tenant limits for a plan tier (one row of ``llms.rate_limits``).

    Token/cost fields use ``int`` (whole tokens) / ``float`` (USD). The rate limiter
    enforces ``requests_per_min`` (pre-request) and the ``*_tokens_per_min`` caps
    (post-hoc debit). Cost caps are carried for completeness (budget enforcement is a
    later WP) so the dataclass is the full Contract-19 ``llms`` block.
    """

    plan: str
    requests_per_min: int
    prompt_tokens_per_min: int
    completion_tokens_per_min: int
    cost_usd_per_hour: float = 0.0
    cost_usd_per_day: float = 0.0
    cost_usd_per_month: float = 0.0


# In-code cold-start fallback — MUST mirror the migration 0004 seed (and the Auth
# plan_defaults `llms` block). Used when the DB is unreachable so the resolver still
# returns sane, plan-appropriate numbers rather than 0/unlimited.
_FALLBACK_LIMITS: dict[str, PlanLimits] = {
    "free": PlanLimits("free", 60, 100_000, 50_000),
    "pro": PlanLimits("pro", 600, 2_000_000, 1_000_000),
    "enterprise": PlanLimits("enterprise", 10_000, 100_000_000, 50_000_000),
}


# ── In-process TTL cache (plan -> (expires_at, PlanLimits)) ─────────────────────
_cache: dict[str, tuple[float, PlanLimits]] = {}


def _cache_get(plan: str) -> PlanLimits | None:
    entry = _cache.get(plan)
    if entry is None:
        return None
    expires_at, limits = entry
    if time.monotonic() >= expires_at:
        _cache.pop(plan, None)
        return None
    return limits


def _cache_put(plan: str, limits: PlanLimits, ttl_seconds: float) -> None:
    _cache[plan] = (time.monotonic() + ttl_seconds, limits)


def clear_cache() -> None:
    """Drop all cached plan->limits entries (test/admin hook)."""
    _cache.clear()


def _normalize_plan(raw: object) -> str | None:
    """Coerce a raw ``plan`` claim to a known tier, or ``None`` if unrecognized."""
    if not isinstance(raw, str):
        return None
    plan = raw.strip().lower()
    return plan if plan in KNOWN_PLANS else None


def _default_limits(settings: Settings) -> PlanLimits:
    """The fail-open / unknown-plan tier."""
    plan = (settings.default_plan or "free").strip().lower()
    return _FALLBACK_LIMITS.get(plan, _FALLBACK_LIMITS["free"])


async def _fetch_from_db(pool: AsyncConnectionPool, plan: str) -> PlanLimits | None:
    """Read the ``llms.rate_limits`` row for ``plan`` (platform-scoped, no RLS).

    Returns ``None`` (not raise) when the pool is missing, the row is absent, or any
    DB error occurs — the caller folds that into the fail-open path.
    """
    if pool is None:
        return None
    sql = """
        SELECT plan, requests_per_min, prompt_tokens_per_min, completion_tokens_per_min,
               cost_usd_per_hour, cost_usd_per_day, cost_usd_per_month
          FROM llms.rate_limits
         WHERE plan = %s
    """
    try:
        async with pool.connection(timeout=2.0) as conn:
            cur = await conn.execute(sql, (plan,))
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — DB down: caller fails open
        logger.warning("rate_limits_db_read_failed", plan=plan, error=str(exc))
        metrics.plan_resolve_failopen_total.labels("db_error").inc()
        return None
    if row is None:
        return None
    return PlanLimits(
        plan=str(row[0]),
        requests_per_min=int(row[1]),
        prompt_tokens_per_min=int(row[2]),
        completion_tokens_per_min=int(row[3]),
        cost_usd_per_hour=float(row[4]),
        cost_usd_per_day=float(row[5]),
        cost_usd_per_month=float(row[6]),
    )


async def _fetch_from_auth(settings: Settings, tenant_id: str, plan: str) -> PlanLimits | None:
    """TODO STUB — HTTP enrichment from Auth ``GET /v1/tenants/{id}/limits``.

    Disabled by default: there is no service-token provider in this codebase yet, so
    the gateway cannot authenticate an outbound call to Auth. When a service-token
    provider lands, implement this with the shared httpx client + a service JWT,
    map the response ``llms`` block onto ``PlanLimits``, and call it as the final
    fallback in :func:`resolve_limits`. Until then it always returns ``None`` so the
    DB row (or the in-code fallback) is authoritative.
    """
    return None


async def resolve_limits(
    principal: Principal,
    *,
    pool: AsyncConnectionPool | None = None,
    settings: Settings | None = None,
) -> PlanLimits:
    """Resolve the effective :class:`PlanLimits` for ``principal``'s tenant. NEVER raises.

    Args:
        principal: the authenticated caller; the ``plan`` claim is read from
            ``principal.raw_claims["plan"]``.
        pool: the app DB pool (``request.app.state.db_pool``) for the ``rate_limits``
            row fallback. ``None`` -> skip the DB step (in-code fallback used).
        settings: app settings; defaults to ``get_settings()`` if omitted.

    Returns:
        A :class:`PlanLimits`. On any failure to resolve a known plan it fails open to
        the default permissive tier (``settings.default_plan``) and counts a metric.
    """
    settings = settings or get_settings()
    ttl = settings.plan_cache_ttl_seconds

    plan = _normalize_plan(principal.raw_claims.get("plan"))
    if plan is None:
        # No usable plan claim — fail open to the default tier (still cached so the
        # whole request path stays cheap). We do NOT guess a tenant's plan from the DB
        # without a plan key; the plan claim (or Auth) is the authority.
        metrics.plan_resolve_failopen_total.labels("no_claim").inc()
        logger.info("plan_resolve_failopen", reason="no_claim", tenant_id=principal.tenant_id)
        return _default_limits(settings)

    cached = _cache_get(plan)
    if cached is not None:
        metrics.plan_cache_hits_total.inc()
        return cached

    limits = await _fetch_from_db(pool, plan) if pool is not None else None  # type: ignore[arg-type]
    if limits is None:
        limits = await _fetch_from_auth(settings, principal.tenant_id, plan)
    if limits is None:
        # Plan is known but neither DB nor Auth gave numbers — use the in-code fallback
        # for THIS plan (not the default tier): the plan claim is trusted.
        limits = _FALLBACK_LIMITS.get(plan)
    if limits is None:
        metrics.plan_resolve_failopen_total.labels("unknown_plan").inc()
        logger.warning("plan_resolve_failopen", reason="unknown_plan", plan=plan)
        return _default_limits(settings)

    _cache_put(plan, limits, ttl)
    return limits
