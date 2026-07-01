"""Model-alias management + per-agent LLM allowlist (orchestrator LLM governance).

Two concerns live here, both tenant-scoped (RLS via ``in_tenant``):

* **Alias CRUD** over ``llms.model_aliases`` — a tenant defines aliases (``smart`` → some
  model, with a ``task_type`` so the orchestrator can pick the right model per sub-agent task),
  exactly one of which is the tenant ``is_default``. Setting a new default atomically demotes the
  previous one (the partial unique index is the safety net).
* **Per-agent allowlist** over ``llms.agent_allowed_llm_aliases`` — restricts WHICH aliases a given
  agent may invoke. An EMPTY allowlist = unrestricted. The gateway enforces it on every chat call
  (:func:`enforce_agent_alias`) so a sub-agent confined to e.g. ``fast`` cannot call ``smart``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from psycopg.rows import dict_row, tuple_row

from ..core.errors import ApiError, ErrorCode
from ..db.pool import in_tenant

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)


# ── alias CRUD ───────────────────────────────────────────────────────────────────────────
async def list_aliases(
    pool: AsyncConnectionPool, tenant_id: str, *, task_type: str | None = None
) -> list[dict[str, Any]]:
    """List aliases visible to the tenant (its own + platform NULL-tenant), newest first.

    Optional ``task_type`` filter. RLS read policy admits own + platform rows.
    """

    async def _q(conn: Any) -> list[dict[str, Any]]:
        sql = (
            "SELECT id::text, tenant_id::text, alias, model_id, provider, "
            "       is_default, task_type, description, created_at "
            "  FROM llms.model_aliases "
            " WHERE (%s::text IS NULL OR task_type = %s) "
            " ORDER BY is_default DESC, created_at DESC"
        )
        cur = await conn.cursor(row_factory=dict_row).execute(sql, (task_type, task_type))
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _q)


async def create_alias(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    alias: str,
    model_id: str,
    provider: str,
    task_type: str | None,
    description: str | None,
    make_default: bool,
) -> dict[str, Any]:
    """Create a tenant alias. The DB trigger makes the FIRST alias default automatically; an
    explicit ``make_default`` demotes any current default first (atomic, one tx)."""

    async def _q(conn: Any) -> dict[str, Any]:
        if make_default:
            await conn.execute(
                "UPDATE llms.model_aliases SET is_default = false "
                " WHERE tenant_id = %s::uuid AND is_default = true",
                (tenant_id,),
            )
        cur = await conn.cursor(row_factory=dict_row).execute(
            "INSERT INTO llms.model_aliases "
            "  (tenant_id, alias, model_id, provider, task_type, description, is_default) "
            "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s) "
            "RETURNING id::text, tenant_id::text, alias, model_id, provider, "
            "          is_default, task_type, description, created_at",
            (tenant_id, alias, model_id, provider, task_type, description, make_default),
        )
        row = await cur.fetchone()
        if row is None:
            raise ApiError(ErrorCode.INTERNAL_ERROR, "Alias insert returned no row.")
        return row

    return await in_tenant(pool, tenant_id, _q)


async def update_alias(
    pool: AsyncConnectionPool,
    tenant_id: str,
    alias: str,
    *,
    make_default: bool | None,
    task_type: str | None,
    description: str | None,
) -> dict[str, Any]:
    """Update a tenant alias's attributes. Setting ``make_default=True`` demotes the current
    default atomically. 404 if the tenant has no such alias."""

    async def _q(conn: Any) -> dict[str, Any]:
        if make_default:
            await conn.execute(
                "UPDATE llms.model_aliases SET is_default = false "
                " WHERE tenant_id = %s::uuid AND is_default = true AND alias <> %s",
                (tenant_id, alias),
            )
        sets = ["is_default = COALESCE(%s, is_default)"]
        args: list[Any] = [make_default]
        sets.append("task_type = COALESCE(%s, task_type)")
        args.append(task_type)
        sets.append("description = COALESCE(%s, description)")
        args.append(description)
        args.extend([tenant_id, alias])
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"UPDATE llms.model_aliases SET {', '.join(sets)} "
            " WHERE tenant_id = %s::uuid AND alias = %s "
            "RETURNING id::text, tenant_id::text, alias, model_id, provider, "
            "          is_default, task_type, description, created_at",
            tuple(args),
        )
        row = await cur.fetchone()
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"No tenant alias '{alias}'.", status_code=404)
        return row

    return await in_tenant(pool, tenant_id, _q)


async def delete_alias(pool: AsyncConnectionPool, tenant_id: str, alias: str) -> None:
    """Delete a tenant alias (no-op if absent; platform aliases are RLS-invisible to writes)."""

    async def _q(conn: Any) -> None:
        await conn.execute(
            "DELETE FROM llms.model_aliases WHERE tenant_id = %s::uuid AND alias = %s",
            (tenant_id, alias),
        )

    await in_tenant(pool, tenant_id, _q)


# ── per-agent allowlist ────────────────────────────────────────────────────────────────────
async def get_agent_aliases(
    pool: AsyncConnectionPool, tenant_id: str, agent_id: str
) -> list[str]:
    """Return the agent's allowed-alias list (empty = unrestricted)."""

    async def _q(conn: Any) -> list[str]:
        cur = await conn.cursor(row_factory=tuple_row).execute(
            "SELECT alias FROM llms.agent_allowed_llm_aliases "
            " WHERE tenant_id = %s::uuid AND agent_id = %s::uuid ORDER BY alias",
            (tenant_id, agent_id),
        )
        return [r[0] for r in await cur.fetchall()]

    return await in_tenant(pool, tenant_id, _q)


async def set_agent_aliases(
    pool: AsyncConnectionPool, tenant_id: str, agent_id: str, aliases: list[str]
) -> list[str]:
    """Full-replace an agent's allowlist (delete-all then insert the cleaned set)."""
    clean = sorted({a.strip() for a in aliases if a.strip()})

    async def _q(conn: Any) -> list[str]:
        await conn.execute(
            "DELETE FROM llms.agent_allowed_llm_aliases "
            " WHERE tenant_id = %s::uuid AND agent_id = %s::uuid",
            (tenant_id, agent_id),
        )
        for alias in clean:
            await conn.execute(
                "INSERT INTO llms.agent_allowed_llm_aliases (tenant_id, agent_id, alias) "
                "VALUES (%s::uuid, %s::uuid, %s) ON CONFLICT DO NOTHING",
                (tenant_id, agent_id, alias),
            )
        return clean

    return await in_tenant(pool, tenant_id, _q)


async def enforce_agent_alias(
    pool: AsyncConnectionPool | None,
    tenant_id: str,
    agent_id: str | None,
    requested_model: str,
) -> None:
    """Reject (403 LLM_ALIAS_NOT_ALLOWED) if the agent has a non-empty allowlist that excludes
    ``requested_model``. No pool / no agent_id / empty allowlist = unrestricted (no-op).

    Fail-soft on a lookup error: a transient DB hiccup must not block a legitimate call (Postgres
    is already the readiness gate, so a genuine outage fails the request elsewhere)."""
    if pool is None or not agent_id:
        return
    try:
        allowed = await get_agent_aliases(pool, tenant_id, agent_id)
    except Exception as exc:  # noqa: BLE001 — never 5xx the call on an allowlist lookup blip
        logger.warning("agent_alias_allowlist_lookup_failed", agent_id=agent_id, error=str(exc))
        return
    if allowed and requested_model not in allowed:
        raise ApiError(
            ErrorCode.LLM_ALIAS_NOT_ALLOWED,
            f"Model/alias '{requested_model}' is not in this agent's allowed LLM list.",
            details={"requested": requested_model, "allowed": allowed},
        )
