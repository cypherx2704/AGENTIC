"""psycopg3 async connection pool + tenant-scoped transaction helper.

The runtime role (``tool_user``) is NOT a superuser and does NOT bypass RLS, so every
tenant-scoped query MUST run inside a transaction that first sets ``app.tenant_id``
via ``SELECT set_config('app.tenant_id', %s, true)`` (the ``true`` makes it
transaction-local — equivalent to ``SET LOCAL``). The RLS policies then admit only
rows for that tenant (plus NULL-tenant platform rows on mixed-scope tables).

``in_platform`` runs a transaction with an EMPTY ``app.tenant_id`` — used by the
background health poller and the platform seed, which operate across all rows. On a
mixed-scope table the read policy admits NULL-tenant (platform) rows when the GUC is
empty; on a tenant-only table no rows are visible (which is correct for those paths).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from psycopg import AsyncConnection
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


async def in_platform[T](
    pool: AsyncConnectionPool,
    fn: Callable[[AsyncConnection], Awaitable[T]],
) -> T:
    """Run ``fn(conn)`` with an EMPTY ``app.tenant_id`` (platform/cross-tenant paths).

    Used by the manifest-health poller (updates ``tool_health`` for every tool) and
    the platform seed. The empty GUC means tenant RLS predicates fall through the
    ``NULLIF(...,'')::uuid`` guard (no tenant rows admitted), while mixed-scope read
    policies still admit NULL-tenant platform rows.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', '', true)")
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
