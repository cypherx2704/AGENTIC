"""Per-API-key ACL enforcement (WP06, Contract-18).

A tenant MAY constrain which models / providers / operations a given Auth-minted
API key is allowed to invoke by inserting rows into ``llms.api_key_acls`` (keyed by
``Principal.api_key_id``). This module loads those rows (RLS-scoped to the caller's
tenant) and decides whether a resolved request is permitted.

SEMANTICS (Contract-18):

* **A key with NO ACL rows is UNRESTRICTED.** The ACL is *opt-in*: the absence of any
  row means the key was never scoped down, so every model/provider/operation is
  allowed. This is the documented default.
* Each of the three array columns is checked **per dimension**: ``NULL`` means "no
  restriction on that dimension"; a non-NULL array must *contain* the requested value
  for that dimension to be permitted by the row.
* When a key has one or more rows, the request is allowed iff **at least one row**
  permits the model AND the provider AND the operation (a row permits a dimension when
  its array is NULL or contains the value). If no single row permits all three, the
  request is rejected with **403 FORBIDDEN** (Contract-2, reason ``ACL_DENIED``).

FAIL-OPEN (allow): the check fails OPEN — i.e. ALLOWS — when there is no DB pool wired
(``pool is None``, the unit-test path) or the ACL load errors (DB unreachable). The ACL
is an authorization *narrowing* on top of an already-authenticated principal; a DB
outage must not lock every key out, and a key with no rows is unrestricted by design,
so "couldn't load any rows" and "no rows exist" collapse to the same allow decision.
Both are logged + counted (``acl_failopen_total``).

The only path that ever raises is: pool present, load succeeded, the key HAS rows, and
none of them permit the request -> 403.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from psycopg import AsyncConnection
from psycopg.rows import tuple_row

from ..core import metrics
from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from ..core.auth import Principal
    from ..core.config import Settings

logger = structlog.get_logger(__name__)

# One row of llms.api_key_acls as loaded: (allowed_models, allowed_providers, allowed_operations).
# Each element is a list[str] (the Postgres TEXT[]) or None ("no restriction on that dimension").
_AclRow = tuple[list[str] | None, list[str] | None, list[str] | None]


async def _load_acl_rows(
    pool: AsyncConnectionPool,
    principal: Principal,
) -> list[_AclRow]:
    """Load the ACL rows for ``principal.api_key_id`` (RLS-scoped to the tenant).

    Runs inside :func:`in_tenant`, so ``app.tenant_id`` is set and RLS on
    ``llms.api_key_acls`` admits ONLY this tenant's rows — the tenant is enforced by
    RLS, not interpolated into the WHERE clause (we still filter by api_key_id).
    """
    sql = """
        SELECT allowed_models, allowed_providers, allowed_operations
          FROM llms.api_key_acls
         WHERE api_key_id = %s
    """

    async def _fn(conn: AsyncConnection) -> list[_AclRow]:
        cur = await conn.cursor(row_factory=tuple_row).execute(sql, (principal.api_key_id,))
        return await cur.fetchall()

    return await in_tenant(pool, principal.tenant_id, _fn)


def _row_permits(allowed: list[str] | None, value: str) -> bool:
    """A single row permits a dimension when its array is NULL (no restriction) or
    contains the requested value."""
    return allowed is None or value in allowed


async def enforce_acl(
    pool: AsyncConnectionPool | None,
    principal: Principal,
    *,
    model: str,
    provider: str,
    operation: str,
    settings: Settings,
) -> None:
    """Enforce the per-key ACL for a resolved request. Raises 403 on deny; else returns.

    Args:
        pool: the app DB pool (``request.app.state.db_pool``). ``None`` -> fail OPEN (allow).
        principal: the authenticated caller; the ACL is keyed by ``principal.api_key_id``.
        model: the RESOLVED model id (post alias-resolution) being invoked.
        provider: the RESOLVED provider for that model.
        operation: ``"chat"`` or ``"embedding"``.
        settings: app settings (carries the ACL master switch).

    Fail-open (ALLOW) when: ACL disabled, no api_key_id on the principal, no DB pool, or
    the ACL load errors. Raises :class:`ApiError` (403 FORBIDDEN, reason ``ACL_DENIED``)
    only when the key has rows and none permits the model+provider+operation triple.
    """
    if not settings.acl_enabled:
        return

    # No api_key_id (e.g. a bare agent / service principal): there is no key to scope,
    # so the per-KEY ACL does not apply. Allow.
    if principal.api_key_id is None:
        return

    if pool is None:
        # Unit-test / no-DB path: fail open (unrestricted default).
        logger.info("acl_check_skipped", reason="no_pool", api_key_id=principal.api_key_id)
        metrics.acl_failopen_total.labels("no_pool").inc()
        return

    try:
        rows = await _load_acl_rows(pool, principal)
    except Exception as exc:  # noqa: BLE001 — DB down/slow: FAIL OPEN (availability wins)
        logger.warning("acl_load_failed", error=str(exc), api_key_id=principal.api_key_id)
        metrics.acl_failopen_total.labels("db_error").inc()
        return

    # No rows for this key == UNRESTRICTED (the Contract-18 default). Allow.
    if not rows:
        return

    # The key is scoped: allow iff at least one row permits all three dimensions.
    for allowed_models, allowed_providers, allowed_operations in rows:
        if (
            _row_permits(allowed_models, model)
            and _row_permits(allowed_providers, provider)
            and _row_permits(allowed_operations, operation)
        ):
            return

    # No row permits the triple — determine the dimension to report (model first, then
    # provider, then operation) for the metric + a clear error detail.
    if not any(_row_permits(r[0], model) for r in rows):
        dimension, requested = "model", model
    elif not any(_row_permits(r[1], provider) for r in rows):
        dimension, requested = "provider", provider
    else:
        dimension, requested = "operation", operation

    metrics.acl_denied_total.labels(dimension).inc()
    logger.info(
        "acl_denied",
        api_key_id=principal.api_key_id,
        dimension=dimension,
        model=model,
        provider=provider,
        operation=operation,
    )
    raise ApiError(
        ErrorCode.FORBIDDEN,
        f"This API key is not permitted to use {dimension} '{requested}'.",
        status_code=403,
        details={
            "reason": "ACL_DENIED",
            "dimension": dimension,
            "model": model,
            "provider": provider,
            "operation": operation,
        },
    )
