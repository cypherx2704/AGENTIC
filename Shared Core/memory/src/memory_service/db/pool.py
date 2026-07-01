"""psycopg3 async connection pool + tenant-scoped transaction helper.

The runtime role (``mem_user``) is NOT a superuser and does NOT bypass RLS, so every
tenant-scoped query MUST run inside a transaction that first sets ``app.tenant_id`` via
``SELECT set_config('app.tenant_id', %s, true)`` (the ``true`` makes it
transaction-local — equivalent to ``SET LOCAL``). The RLS policies then admit only rows
for that tenant.
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
    The whole memory write path (store, dedup-bump, GDPR wipe) runs through this so the
    DB write + outbox event are atomic.
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
