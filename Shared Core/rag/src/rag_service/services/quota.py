"""Quota enforcement (Component 5d / Contract-19 limits).

Resolves a tenant's RAG limits from the JWT ``plan`` claim (PRIMARY, no network) with a
short in-process TTL cache + an in-code fallback table, exactly like the llms auth_client.
Enforces the four first-cycle RAG quotas:

  * ``kbs_max``              — count cap on knowledge bases (413 QUOTA_EXCEEDED).
  * ``documents_per_kb_max`` — count cap on documents in one KB (413).
  * ``queries_per_min``      — Valkey fixed-window rate cap (429 + Retry-After).
  * ``storage_bytes_max``    — at-rest bytes cap, checked at ingest write time (413).

ALL checks FAIL OPEN: when a plan cannot be resolved, the DB pool / Valkey are absent, or
a check errors, the request is ALLOWED (availability wins) and a metric is bumped. The JWT
``rag`` limits block, when present, overrides the plan default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..core.auth import Principal
from ..core.config import Settings
from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant
from ..db.valkey import ValkeyClient

logger = structlog.get_logger(__name__)

KNOWN_PLANS = ("free", "pro", "enterprise")


@dataclass(frozen=True)
class RagLimits:
    plan: str
    kbs_max: int
    documents_per_kb_max: int
    queries_per_min: int
    storage_bytes_max: int


# In-code cold-start fallback (mirrors the Auth plan_defaults `rag` block). Generous so a
# missing plan never spuriously blocks. -1 sentinel = unlimited.
_FALLBACK_LIMITS: dict[str, RagLimits] = {
    "free": RagLimits("free", kbs_max=5, documents_per_kb_max=100, queries_per_min=60,
                      storage_bytes_max=1 * 1024 * 1024 * 1024),  # 1 GiB
    "pro": RagLimits("pro", kbs_max=50, documents_per_kb_max=10_000, queries_per_min=600,
                     storage_bytes_max=100 * 1024 * 1024 * 1024),  # 100 GiB
    "enterprise": RagLimits("enterprise", kbs_max=1000, documents_per_kb_max=1_000_000,
                            queries_per_min=10_000, storage_bytes_max=10 * 1024 * 1024 * 1024 * 1024),
}

_cache: dict[str, tuple[float, RagLimits]] = {}


def clear_cache() -> None:
    _cache.clear()


def _normalize_plan(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    plan = raw.strip().lower()
    return plan if plan in KNOWN_PLANS else None


def _default_limits(settings: Settings) -> RagLimits:
    plan = (settings.default_plan or "free").strip().lower()
    return _FALLBACK_LIMITS.get(plan, _FALLBACK_LIMITS["free"])


def _limits_from_claims(principal: Principal, plan: str) -> RagLimits:
    """Build limits from a `rag` limits block in the JWT if present, else the plan default."""
    base = _FALLBACK_LIMITS.get(plan, _FALLBACK_LIMITS["free"])
    raw = principal.raw_claims.get("limits")
    rag_block = raw.get("rag") if isinstance(raw, dict) else None
    if not isinstance(rag_block, dict):
        return base
    return RagLimits(
        plan=plan,
        kbs_max=int(rag_block.get("kbs_max", base.kbs_max)),
        documents_per_kb_max=int(rag_block.get("documents_per_kb_max", base.documents_per_kb_max)),
        queries_per_min=int(rag_block.get("queries_per_min", base.queries_per_min)),
        storage_bytes_max=int(rag_block.get("storage_bytes_max", base.storage_bytes_max)),
    )


def resolve_limits(principal: Principal, *, settings: Settings) -> RagLimits:
    """Resolve effective RAG limits for the principal's plan. NEVER raises (fail-open)."""
    plan = _normalize_plan(principal.raw_claims.get("plan"))
    if plan is None:
        metrics.quota_failopen_total.labels("no_plan").inc()
        return _default_limits(settings)
    entry = _cache.get(plan)
    now = time.monotonic()
    if entry is not None and entry[0] > now:
        return entry[1]
    limits = _limits_from_claims(principal, plan)
    _cache[plan] = (now + settings.plan_cache_ttl_seconds, limits)
    return limits


def _unlimited(value: int) -> bool:
    return value < 0


# ── Count checks (413 over cap) ─────────────────────────────────────────────────
async def enforce_kbs_max(
    pool: AsyncConnectionPool | None, principal: Principal, *, settings: Settings
) -> None:
    if not settings.quota_enabled or pool is None:
        if pool is None:
            metrics.quota_failopen_total.labels("no_pool").inc()
        return
    limits = resolve_limits(principal, settings=settings)
    if _unlimited(limits.kbs_max):
        return
    try:
        async def _txn(conn: object) -> int:
            cur = await conn.execute("SELECT COUNT(*) FROM rag.knowledge_bases")  # type: ignore[attr-defined]
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        count = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001 — DB error: fail open
        logger.warning("quota_kbs_check_failed", error=str(exc))
        return
    if count >= limits.kbs_max:
        metrics.quota_rejected_total.labels("kbs_max").inc()
        raise ApiError(
            ErrorCode.QUOTA_EXCEEDED,
            f"Knowledge-base limit reached ({limits.kbs_max}).",
            status_code=413,
            details={"dimension": "kbs_max", "limit": limits.kbs_max, "current": count},
        )


async def enforce_documents_per_kb_max(
    pool: AsyncConnectionPool | None, principal: Principal, kb_id: str, *, settings: Settings
) -> None:
    if not settings.quota_enabled or pool is None:
        if pool is None:
            metrics.quota_failopen_total.labels("no_pool").inc()
        return
    limits = resolve_limits(principal, settings=settings)
    if _unlimited(limits.documents_per_kb_max):
        return
    try:
        async def _txn(conn: object) -> int:
            cur = await conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM rag.documents WHERE kb_id = %s", (kb_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        count = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001 — DB error: fail open
        logger.warning("quota_docs_check_failed", error=str(exc))
        return
    if count >= limits.documents_per_kb_max:
        metrics.quota_rejected_total.labels("documents_per_kb_max").inc()
        raise ApiError(
            ErrorCode.QUOTA_EXCEEDED,
            f"Document limit for this KB reached ({limits.documents_per_kb_max}).",
            status_code=413,
            details={
                "dimension": "documents_per_kb_max",
                "limit": limits.documents_per_kb_max,
                "current": count,
            },
        )


async def enforce_storage_bytes_max(
    pool: AsyncConnectionPool | None,
    principal: Principal,
    *,
    additional_bytes: int,
    settings: Settings,
) -> None:
    """Reject (413) when current at-rest bytes + ``additional_bytes`` exceed the cap."""
    if not settings.quota_enabled or pool is None:
        if pool is None:
            metrics.quota_failopen_total.labels("no_pool").inc()
        return
    limits = resolve_limits(principal, settings=settings)
    if _unlimited(limits.storage_bytes_max):
        return
    try:
        async def _txn(conn: object) -> int:
            cur = await conn.execute("SELECT COUNT(*) FROM rag.chunks")  # type: ignore[attr-defined]
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        chunk_count = await in_tenant(pool, principal.tenant_id, _txn)
    except Exception as exc:  # noqa: BLE001 — DB error: fail open
        logger.warning("quota_storage_check_failed", error=str(exc))
        return
    current_bytes = chunk_count * 24 * 1024  # ~24 KiB/chunk at rest (matches PgVectorAdapter)
    if current_bytes + additional_bytes > limits.storage_bytes_max:
        metrics.quota_rejected_total.labels("storage_bytes_max").inc()
        raise ApiError(
            ErrorCode.QUOTA_EXCEEDED,
            f"Storage limit reached ({limits.storage_bytes_max} bytes).",
            status_code=413,
            details={
                "dimension": "storage_bytes_max",
                "limit": limits.storage_bytes_max,
                "current": current_bytes,
            },
        )


# ── Rate check (429 over window) ────────────────────────────────────────────────
async def enforce_queries_per_min(
    valkey: ValkeyClient | None, principal: Principal, *, settings: Settings
) -> None:
    """Fixed-window queries/min cap. 429 over limit. FAILS OPEN when Valkey is absent/down."""
    if not settings.quota_enabled:
        return
    limits = resolve_limits(principal, settings=settings)
    if _unlimited(limits.queries_per_min):
        return
    if valkey is None:
        metrics.quota_failopen_total.labels("valkey_unavailable").inc()
        return
    window = int(time.time()) // settings.quota_window_seconds
    key = f"{settings.quota_key_prefix}q:{principal.tenant_id}:{window}"
    try:
        count = await valkey.incr_with_expire(
            key,
            ttl_seconds=settings.quota_window_seconds,
            timeout_seconds=settings.quota_valkey_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — Valkey down: fail open
        logger.warning("quota_rate_failopen", error=str(exc))
        metrics.quota_failopen_total.labels("valkey_unavailable").inc()
        return
    if count > limits.queries_per_min:
        metrics.quota_rejected_total.labels("queries_per_min").inc()
        raise ApiError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            f"Query rate limit exceeded ({limits.queries_per_min}/min).",
            details={"dimension": "queries_per_min", "limit": limits.queries_per_min},
            headers={"Retry-After": str(settings.quota_window_seconds)},
        )
