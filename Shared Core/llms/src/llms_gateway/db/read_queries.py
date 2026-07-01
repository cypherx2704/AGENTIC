"""Tenant-scoped read aggregations for the WP05 read surface (Contract-19 / 1d).

Every function here runs inside :func:`llms_gateway.db.pool.in_tenant`, so the
``app.tenant_id`` GUC is set for the transaction and RLS on ``llms.usage_records``
admits ONLY the caller's rows (Contract 13 — the tenant is taken from the JWT
Principal, never from a body/param). The tenant is therefore never interpolated
into a WHERE clause here: the RLS policy enforces it.

``group_by`` is mapped through a FIXED allowlist (``_GROUP_BY_SQL``) to a literal,
trusted SQL expression — user input never reaches the query as a string. User
values (the optional ISO ``from``/``to`` bounds and the row LIMIT) are passed as
psycopg parameters.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .pool import in_tenant

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# ── group_by allowlist ───────────────────────────────────────────────────────
# Maps each accepted group_by token to (select_alias, trusted_sql_expr). The keys
# are the ONLY values the API accepts (validated against this dict -> 422 on miss);
# the SQL expressions are literals defined here, never derived from user input.
_GROUP_BY_SQL: dict[str, str] = {
    "model": "model",
    "agent": "agent_id::text",
    "api_key": "api_key_id::text",
    "date": "(created_at AT TIME ZONE 'UTC')::date::text",
}

# Allowlisted group_by keys (stable order for response determinism).
GROUP_BY_KEYS: tuple[str, ...] = ("model", "agent", "api_key", "date")

# Tenant-visible model aliases (RLS admits the tenant's own rows + NULL platform
# rows). Returned newest-first is irrelevant; ordered by alias for stable output.
_ALIASES_SQL = """
    SELECT alias, model_id, provider, (tenant_id IS NULL) AS is_platform
      FROM llms.model_aliases
     ORDER BY model_id, alias
"""


def _select_clause(group_by: list[str]) -> tuple[str, list[str]]:
    """Build the validated SELECT projection for the requested group_by columns.

    Returns ``(projection_sql, selected_aliases)``. ``group_by`` is assumed already
    validated against ``_GROUP_BY_SQL`` by the caller (the API layer raises 422 on
    an unknown key BEFORE reaching here)."""
    aliases = list(dict.fromkeys(group_by))  # de-dupe, preserve order
    cols = [f"{_GROUP_BY_SQL[a]} AS {a}" for a in aliases]
    return ", ".join(cols), aliases


def _group_clause(group_by: list[str]) -> str:
    """Trusted GROUP BY expression list (allowlisted expressions only)."""
    aliases = list(dict.fromkeys(group_by))
    return ", ".join(_GROUP_BY_SQL[a] for a in aliases)


async def _aggregate(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    sum_select: str,
    group_by: list[str],
    ts_from: datetime | None,
    ts_to: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Run one RLS-scoped GROUP BY aggregation over ``llms.usage_records``.

    ``sum_select`` is a trusted, literal projection of the aggregate columns
    (defined by the two public callers below — never user-derived). The tenant is
    enforced by RLS (``app.tenant_id``), not a WHERE clause.
    """
    projection, _ = _select_clause(group_by)
    group_expr = _group_clause(group_by)

    where: list[str] = []
    params: list[Any] = []
    if ts_from is not None:
        where.append("created_at >= %s")
        params.append(ts_from)
    if ts_to is not None:
        where.append("created_at < %s")
        params.append(ts_to)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = (
        f"SELECT {projection}, {sum_select} "  # noqa: S608 — projection/sum are allowlisted literals, not user input
        f"FROM llms.usage_records{where_sql} "
        f"GROUP BY {group_expr} "
        f"ORDER BY {group_expr} "
        "LIMIT %s"
    )
    params.append(limit)

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(sql, params)
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def aggregate_usage(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    group_by: list[str],
    ts_from: datetime | None,
    ts_to: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Grouped token sums + request_count for the caller's tenant."""
    sum_select = (
        "COALESCE(SUM(prompt_tokens), 0)::bigint     AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens, "
        "COALESCE(SUM(total_tokens), 0)::bigint      AS total_tokens, "
        "COUNT(*)::bigint                            AS request_count"
    )
    return await _aggregate(
        pool,
        tenant_id,
        sum_select=sum_select,
        group_by=group_by,
        ts_from=ts_from,
        ts_to=ts_to,
        limit=limit,
    )


async def aggregate_cost(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    group_by: list[str],
    ts_from: datetime | None,
    ts_to: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Grouped cost_usd sum (+ token totals + request_count) for the tenant."""
    sum_select = (
        "COALESCE(SUM(cost_usd), 0)::numeric         AS cost_usd, "
        "COALESCE(SUM(prompt_tokens), 0)::bigint     AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens, "
        "COALESCE(SUM(total_tokens), 0)::bigint      AS total_tokens, "
        "COUNT(*)::bigint                            AS request_count"
    )
    return await _aggregate(
        pool,
        tenant_id,
        sum_select=sum_select,
        group_by=group_by,
        ts_from=ts_from,
        ts_to=ts_to,
        limit=limit,
    )


async def fetch_tenant_aliases(
    pool: AsyncConnectionPool,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Return aliases visible to the tenant (own + NULL-tenant platform rows).

    RLS on ``llms.model_aliases`` (policy ``p_model_aliases_read``) admits the
    tenant's rows and platform rows (tenant_id IS NULL), so this is genuinely
    per-tenant — a tenant's private aliases are not leaked to others.
    """

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(_ALIASES_SQL)
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)
