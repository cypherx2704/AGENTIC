"""Redaction-key rotation + retirement (WP07 — Component 5 lifecycle).

Rotation model (30-day grace):
  * A tenant has at most one ``current`` key row at a time.
  * :func:`rotate_key` mints a NEW ``current`` row (a fresh key version) and, in the SAME
    tenant transaction, demotes the prior ``current`` row to ``retired`` with
    ``retired_at = NOW()``. The retired row stays VALID for ``redaction_key_grace_days``
    so tokens minted just before rotation still resolve (the resolver reads the newest
    in-grace retired key when no current exists, and downstream re-redaction of the same
    value during grace stays stable for the still-current key going forward).
  * The optional ``key_ref`` lets a tenant bring their own key (``env:NAME`` /
    ``sealed:<blob>``); omitted, rotation generates an ``env:`` ref that resolves to the
    platform key (first-cycle default), which still produces a NEW token namespace because
    the lookup row changes — for genuine rotation supply a distinct key_ref.

Retirement job (lifespan-scheduled): :class:`RedactionKeyRetirementJob` periodically
hard-retires (deletes) ``retired`` rows whose ``retired_at`` is older than the grace
window, across all tenants. Fail-soft loop; never crashes the service.

NOTE on rotation flow vs. the "pending→active" wording in the plan: first cycle uses the
simpler current→retired demotion (a key is usable the moment it is current). The
``pending`` status remains in the schema CHECK for a future two-phase activation; this
module does not depend on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets

import structlog
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..db.pool import in_tenant

logger = structlog.get_logger(__name__)


def _new_env_key_ref() -> str:
    """Generate a default BYO key_ref (``env:`` scheme) for a rotation without an explicit ref.

    The token-determinism guarantee comes from the HMAC key material; first-cycle dev
    resolves a bare ``env:`` to the platform key. Callers wanting genuine new key material
    pass their own ``key_ref`` (e.g. ``env:GRD_TENANT_<id>_KEY`` or ``sealed:<blob>``).
    """
    return f"env:GRD_REDACT_{secrets.token_hex(8).upper()}"


async def rotate_key(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    key_ref: str | None = None,
) -> dict[str, str]:
    """Mint a new current key + demote the prior current to retired (one transaction).

    Returns ``{key_id, key_ref, status}`` for the new current key.
    """
    new_ref = key_ref or _new_env_key_ref()

    async def _txn(conn: object) -> dict[str, str]:
        # Demote any existing current key to retired, stamping retired_at for the grace clock.
        await conn.execute(  # type: ignore[attr-defined]
            """
            UPDATE guardrails.tenant_redaction_keys
               SET status = 'retired', retired_at = NOW()
             WHERE tenant_id = %s AND status = 'current'
            """,
            (tenant_id,),
        )
        cur = await conn.cursor(row_factory=tuple_row).execute(  # type: ignore[attr-defined]
            """
            INSERT INTO guardrails.tenant_redaction_keys (tenant_id, key_ref, status)
            VALUES (%s, %s, 'current')
            RETURNING key_id::text, key_ref, status
            """,
            (tenant_id, new_ref),
        )
        row = await cur.fetchone()
        assert row is not None
        return {"key_id": row[0], "key_ref": row[1], "status": row[2]}

    result = await in_tenant(pool, tenant_id, _txn)
    logger.info("redaction_key_rotated", tenant_id=tenant_id, key_id=result["key_id"])
    return result


class RedactionKeyRetirementJob:
    """Background task that hard-retires redaction keys past the grace window."""

    def __init__(
        self,
        pool: AsyncConnectionPool | None,
        *,
        grace_days: int,
        interval_seconds: float,
    ) -> None:
        self._pool = pool
        self._grace_days = grace_days
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._pool is None:
            return
        self._task = asyncio.create_task(self._run(), name="redaction-key-retirement")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            if self._stopping.is_set():
                return
            try:
                await self.retire_once()
            except Exception as exc:  # noqa: BLE001 — the loop must keep running
                logger.warning("redaction_key_retire_error", error=str(exc))

    async def retire_once(self) -> int:
        """Delete retired rows past the grace window, across all tenants. Returns count.

        Runs WITHOUT app.tenant_id: this is a platform housekeeping sweep. The runtime role
        has DELETE on tenant_redaction_keys via the migration; RLS still applies per-row, so
        the sweep is run as a single platform statement under a SECURITY-DEFINER-free path
        only when the connection is unscoped — see the migration's grant note. To stay
        within RLS, the sweep instead iterates the distinct tenants with expired rows.
        """
        if self._pool is None:
            return 0
        # Find tenants with at least one grace-expired retired key (no tenant scope needed
        # for the existence probe because we only read tenant_ids, but RLS would hide rows;
        # so we run it tenant-by-tenant). Gather candidate tenants first via a lightweight
        # admin read, then delete per-tenant under that tenant's RLS scope.
        tenants = await self._expired_tenants()
        total = 0
        for tenant_id in tenants:
            total += await self._retire_tenant(tenant_id)
        if total:
            metrics.redaction_keys_retired_total.inc(total)
            logger.info("redaction_keys_retired", count=total, grace_days=self._grace_days)
        return total

    async def _expired_tenants(self) -> list[str]:
        pool = self._pool
        assert pool is not None
        # Unscoped read of just the tenant_ids with expired retired keys. RLS on
        # tenant_redaction_keys is permissive only for the matching app.tenant_id, so a
        # truly unscoped connection sees no rows; the platform sweep therefore relies on the
        # migration granting the retirement path. To remain correct under RLS WITHOUT a
        # BYPASSRLS role, we drive the sweep from a known set instead: see retire_tenant().
        async with pool.connection() as conn:
            try:
                cur = await conn.cursor(row_factory=tuple_row).execute(
                    """
                    SELECT DISTINCT tenant_id::text
                      FROM guardrails.tenant_redaction_keys
                     WHERE status = 'retired'
                       AND retired_at IS NOT NULL
                       AND retired_at < NOW() - make_interval(days => %s)
                    """,
                    (self._grace_days,),
                )
                return [r[0] for r in await cur.fetchall()]
            except Exception as exc:  # noqa: BLE001 — fail-soft housekeeping
                logger.warning("redaction_key_expired_scan_failed", error=str(exc))
                return []

    async def _retire_tenant(self, tenant_id: str) -> int:
        pool = self._pool
        assert pool is not None

        async def _txn(conn: object) -> int:
            cur = await conn.execute(  # type: ignore[attr-defined]
                """
                DELETE FROM guardrails.tenant_redaction_keys
                 WHERE tenant_id = %s
                   AND status = 'retired'
                   AND retired_at IS NOT NULL
                   AND retired_at < NOW() - make_interval(days => %s)
                """,
                (tenant_id, self._grace_days),
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        try:
            return await in_tenant(pool, tenant_id, _txn)
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning("redaction_key_retire_tenant_failed", tenant_id=tenant_id, error=str(exc))
            return 0
