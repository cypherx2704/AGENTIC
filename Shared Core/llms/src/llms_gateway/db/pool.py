"""psycopg3 async connection pool + tenant-scoped transaction helper.

The runtime role (``llms_user``) is NOT a superuser and does NOT bypass RLS, so every
tenant-scoped query MUST run inside a transaction that first sets ``app.tenant_id``
via ``SELECT set_config('app.tenant_id', %s, true)`` (the ``true`` makes it
transaction-local — equivalent to ``SET LOCAL``). The RLS policies then admit only
rows for that tenant (plus NULL-tenant platform rows on mixed-scope tables).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


def create_pool(database_url: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """Create (without opening) an AsyncConnectionPool. Caller opens it in the lifespan."""
    return AsyncConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
    )


async def in_tenant[T](
    pool: AsyncConnectionPool,
    tenant_id: str,
    fn: Callable[[AsyncConnection], Awaitable[T]],
) -> T:
    """Run ``fn(conn)`` inside one transaction with ``app.tenant_id`` set for RLS.

    Commits on success, rolls back on error (psycopg ``async with conn.transaction()``).
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        return await fn(conn)


async def readyz_ping(pool: AsyncConnectionPool) -> bool:
    """Return True if a trivial ``SELECT 1`` succeeds (readiness gate)."""
    try:
        async with pool.connection(timeout=2.0) as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 — readiness must never raise
        logger.warning("db_ping_failed", error=str(exc))
        return False


# ── platform-scoped reads (no RLS — provider_pricing, model_aliases platform rows) ──
async def fetch_pricing(
    pool: AsyncConnectionPool,
) -> list[tuple[str, str, Any, Any, Any, Any]]:
    """Return the latest-effective pricing row per (provider, model)."""
    sql = """
        SELECT DISTINCT ON (provider, model)
               provider, model,
               input_cost_per_1k_tokens, output_cost_per_1k_tokens,
               cached_input_cost_per_1k_tokens, cache_creation_cost_per_1k_tokens
          FROM llms.provider_pricing
         ORDER BY provider, model, effective_from DESC
    """
    async with pool.connection(timeout=2.0) as conn:
        cur = await conn.cursor(row_factory=tuple_row).execute(sql)
        return await cur.fetchall()


async def fetch_aliases(
    pool: AsyncConnectionPool,
) -> list[tuple[str | None, str, str, str]]:
    """Return the PLATFORM model aliases as (tenant_id, alias, model_id, provider).

    This runs on a bare (no ``app.tenant_id``) connection, so RLS
    (``p_model_aliases_read``) admits ONLY the platform rows (``tenant_id IS NULL``).
    Tenant-owned aliases are deliberately NOT returned here — a global preload cannot
    see them without bypassing RLS, and caching every tenant's aliases in one process
    map is neither scalable nor isolation-safe. Tenant aliases are resolved on demand,
    inside the caller's own tenant context, via :func:`fetch_tenant_alias`.
    """
    sql = "SELECT tenant_id::text, alias, model_id, provider FROM llms.model_aliases"
    async with pool.connection(timeout=2.0) as conn:
        cur = await conn.cursor(row_factory=tuple_row).execute(sql)
        return await cur.fetchall()


async def fetch_tenant_alias(
    pool: AsyncConnectionPool,
    tenant_id: str,
    alias: str,
) -> tuple[str, str] | None:
    """Resolve one alias for a specific tenant, honouring RLS.

    Runs inside ``in_tenant`` so ``app.tenant_id`` is set and the RLS read policy admits
    the tenant's own ``model_aliases`` row (a tenant alias SHADOWS a platform one of the
    same name — we filter to ``tenant_id = <this tenant>`` so we return the tenant-owned
    row specifically, not the platform default). Returns ``(provider, model_id)`` or
    ``None`` when the tenant has no alias by that name.
    """

    async def _q(conn: AsyncConnection) -> tuple[str, str] | None:
        cur = await conn.cursor(row_factory=tuple_row).execute(
            """
            SELECT provider, model_id
              FROM llms.model_aliases
             WHERE alias = %s
               AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
             LIMIT 1
            """,
            (alias,),
        )
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    return await in_tenant(pool, tenant_id, _q)


async def fetch_pricing_max_updated_at(pool: AsyncConnectionPool) -> Any | None:
    """Return MAX(updated_at) across ``llms.provider_pricing`` (the pricing-data age clock).

    Platform-scoped (no RLS). Returns a ``datetime`` (tz-aware) or ``None`` when the table
    is empty. Used by the pricing-staleness watchdog (WP06).
    """
    sql = "SELECT MAX(updated_at) FROM llms.provider_pricing"
    async with pool.connection(timeout=2.0) as conn:
        cur = await conn.cursor(row_factory=tuple_row).execute(sql)
        row = await cur.fetchone()
    return row[0] if row else None


async def fetch_capabilities(
    pool: AsyncConnectionPool,
) -> list[tuple[str, str, int, int, bool, bool, bool, int | None, bool]]:
    """Return all model capability rows from ``llms.model_capabilities``."""
    sql = """
        SELECT model_id, provider, max_tokens_cap, context_window,
               supports_vision, supports_tools, supports_streaming, embedding_dim,
               native_tool_use
          FROM llms.model_capabilities
    """
    async with pool.connection(timeout=2.0) as conn:
        cur = await conn.cursor(row_factory=tuple_row).execute(sql)
        return await cur.fetchall()
