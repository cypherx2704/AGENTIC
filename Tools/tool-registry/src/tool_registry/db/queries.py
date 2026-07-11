"""Registry data-access: discovery, registration, version retention, health, seed.

All tenant-scoped reads/writes run inside :func:`tool_registry.db.pool.in_tenant`, so
``app.tenant_id`` is set for the transaction and RLS on the ``tools`` /
``tool_versions`` / ``tool_capabilities`` tables admits ONLY the caller's rows plus
NULL-tenant platform rows on the read path. The tenant is taken from the JWT
Principal (Contract 13) and set as the RLS GUC — it is never interpolated into a
WHERE clause.

Cross-tenant write safety (the "marketplace hole"): the WITH CHECK half of every
tenant policy rejects an INSERT/UPDATE that names a tenant_id other than the GUC, so
even a row this code tried to write for another tenant would be refused by Postgres.
This module always writes ``tenant_id`` = the GUC tenant, so it is compatible with
those policies by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .pool import in_platform, in_tenant

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# Active-version statuses (a version chain row counts toward retention while active).
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"


# ── Discovery ─────────────────────────────────────────────────────────────────
_LIST_TOOLS_SQL = """
    SELECT t.tool_id, t.name, t.tenant_id::text AS tenant_id, t.status,
           t.latest_version, (t.tenant_id IS NULL) AS is_platform
      FROM tools t
     ORDER BY t.name, (t.tenant_id IS NULL)  -- tenant row (FALSE) sorts before platform (TRUE)
     LIMIT %s
"""


async def list_visible_tools(
    pool: AsyncConnectionPool, tenant_id: str, *, limit: int
) -> list[dict[str, Any]]:
    """Return tools visible to the tenant (own + platform), RLS-scoped.

    RLS admits the tenant's own rows AND platform rows (tenant_id IS NULL). The rows
    are ordered so that for a given name the tenant's row precedes the platform row;
    the API layer applies tenant-priority shadowing on top.
    """

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(_LIST_TOOLS_SQL, (limit,))
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def get_tool_rows_by_name(
    pool: AsyncConnectionPool, tenant_id: str, name: str
) -> list[dict[str, Any]]:
    """Return the tool row(s) for ``name`` visible to the tenant (own + platform)."""

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT tool_id, name, tenant_id::text AS tenant_id, status, latest_version,
                   (tenant_id IS NULL) AS is_platform
              FROM tools
             WHERE name = %s
            """,
            (name,),
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def get_version(
    pool: AsyncConnectionPool, tenant_id: str, tool_id: str, version: str | None
) -> dict[str, Any] | None:
    """Resolve a tool's version row: a specific ``version`` if given, else the latest active.

    Returns the version row (incl. resolved manifest JSONB) or ``None`` if no matching
    active version exists.
    """

    async def _fn(conn: AsyncConnection) -> dict[str, Any] | None:
        if version is not None:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT version, manifest, status, created_at
                  FROM tool_versions
                 WHERE tool_id = %s AND version = %s AND status = %s
                """,
                (tool_id, version, STATUS_ACTIVE),
            )
        else:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT version, manifest, status, created_at
                  FROM tool_versions
                 WHERE tool_id = %s AND status = %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (tool_id, STATUS_ACTIVE),
            )
        return await cur.fetchone()

    return await in_tenant(pool, tenant_id, _fn)


async def get_capabilities(
    pool: AsyncConnectionPool, tenant_id: str, tool_id: str
) -> list[dict[str, Any]]:
    """Return the declared capability/scope rows for a tool (RLS-scoped)."""

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT capability, required_scope
              FROM tool_capabilities
             WHERE tool_id = %s
             ORDER BY capability
            """,
            (tool_id,),
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def get_health(
    pool: AsyncConnectionPool, tenant_id: str, tool_id: str
) -> dict[str, Any] | None:
    """Return the ``tool_health`` row for a tool (RLS-scoped), or ``None``."""

    async def _fn(conn: AsyncConnection) -> dict[str, Any] | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT status, last_etag, consecutive_failures, last_polled
              FROM tool_health
             WHERE tool_id = %s
            """,
            (tool_id,),
        )
        return await cur.fetchone()

    return await in_tenant(pool, tenant_id, _fn)


# ── Registration ──────────────────────────────────────────────────────────────
async def create_tool_with_version(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    name: str,
    version: str,
    manifest: dict[str, Any],
    capabilities: list[tuple[str, str]],
) -> dict[str, Any]:
    """Register a NEW tool + its first version + capability rows in one transaction.

    All rows are written with ``tenant_id`` = the GUC tenant, so the WITH CHECK halves
    of the RLS policies accept them. A duplicate (tenant_id, name) raises a unique
    violation which the API maps to 409 CONFLICT.
    """

    async def _fn(conn: AsyncConnection) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            INSERT INTO tools (tenant_id, name, status, latest_version)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, 'active', %s)
            RETURNING tool_id, name, tenant_id::text AS tenant_id, status, latest_version
            """,
            (name, version),
        )
        tool = await cur.fetchone()
        assert tool is not None
        await _insert_version(conn, tool["tool_id"], version, manifest)
        await _replace_capabilities(conn, tool["tool_id"], capabilities)
        await _init_health(conn, tool["tool_id"])
        return tool

    return await in_tenant(pool, tenant_id, _fn)


async def add_version(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    tool_id: str,
    version: str,
    manifest: dict[str, Any],
    capabilities: list[tuple[str, str]],
    max_active_versions: int,
) -> dict[str, Any]:
    """Append a new active version to an existing tool, enforcing retention.

    After inserting the version we count active versions; if it exceeds
    ``max_active_versions`` we retire the OLDEST active version(s) down to the cap.
    ``latest_version`` on the parent tool is advanced and capabilities are refreshed
    from the new manifest. Returns ``{version, retired: [..]}``.
    """

    async def _fn(conn: AsyncConnection) -> dict[str, Any]:
        await _insert_version(conn, tool_id, version, manifest)
        await conn.execute(
            "UPDATE tools SET latest_version = %s WHERE tool_id = %s", (version, tool_id)
        )
        await _replace_capabilities(conn, tool_id, capabilities)
        retired = await _enforce_retention(conn, tool_id, max_active_versions)
        return {"version": version, "retired": retired}

    return await in_tenant(pool, tenant_id, _fn)


async def _insert_version(
    conn: AsyncConnection, tool_id: str, version: str, manifest: dict[str, Any]
) -> None:
    await conn.execute(
        """
        INSERT INTO tool_versions (tenant_id, tool_id, version, manifest, status)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, 'active')
        """,
        (tool_id, version, Jsonb(manifest)),
    )


async def _replace_capabilities(
    conn: AsyncConnection, tool_id: str, capabilities: list[tuple[str, str]]
) -> None:
    """Refresh the tool's capability rows to match the latest manifest."""
    await conn.execute("DELETE FROM tool_capabilities WHERE tool_id = %s", (tool_id,))
    for capability, required_scope in capabilities:
        await conn.execute(
            """
            INSERT INTO tool_capabilities (tenant_id, tool_id, capability, required_scope)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s)
            """,
            (tool_id, capability, required_scope),
        )


async def _init_health(conn: AsyncConnection, tool_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO tool_health (tenant_id, tool_id, status, consecutive_failures)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, 'active', 0)
        ON CONFLICT (tool_id) DO NOTHING
        """,
        (tool_id,),
    )


async def _enforce_retention(
    conn: AsyncConnection, tool_id: str, max_active_versions: int
) -> list[str]:
    """Retire the oldest active versions beyond ``max_active_versions``; return retired list."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT version
          FROM tool_versions
         WHERE tool_id = %s AND status = %s
         ORDER BY created_at DESC
        """,
        (tool_id, STATUS_ACTIVE),
    )
    active = [r["version"] for r in await cur.fetchall()]
    to_retire = active[max_active_versions:]  # everything past the newest N
    for version in to_retire:
        await conn.execute(
            "UPDATE tool_versions SET status = %s WHERE tool_id = %s AND version = %s",
            (STATUS_RETIRED, tool_id, version),
        )
    return to_retire


# ── Health persistence (platform-scoped: poller updates every tool) ────────────
async def update_health(
    pool: AsyncConnectionPool,
    *,
    tool_id: str,
    status: str,
    consecutive_failures: int,
    last_etag: str | None,
    manifest: dict[str, Any] | None,
) -> None:
    """Persist a health-poll outcome for a tool (platform-scoped UPSERT).

    Runs with an empty ``app.tenant_id`` (the poller spans all tenants). When the poll
    returned a changed manifest we also refresh the cached manifest on the latest
    active version row. tool_health rows are written without RLS gating on the GUC
    tenant because the table policy admits the poller (see migration).
    """

    async def _fn(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            INSERT INTO tool_health (tool_id, status, consecutive_failures, last_etag, last_polled)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (tool_id) DO UPDATE
              SET status = EXCLUDED.status,
                  consecutive_failures = EXCLUDED.consecutive_failures,
                  last_etag = COALESCE(EXCLUDED.last_etag, tool_health.last_etag),
                  last_polled = NOW()
            """,
            (tool_id, status, consecutive_failures, last_etag),
        )
        if manifest is not None:
            await conn.execute(
                """
                UPDATE tool_versions tv
                   SET manifest = %s
                 WHERE tv.tool_id = %s
                   AND tv.status = 'active'
                   AND tv.created_at = (
                       SELECT MAX(created_at) FROM tool_versions
                        WHERE tool_id = %s AND status = 'active'
                   )
                """,
                (Jsonb(manifest), tool_id, tool_id),
            )

    await in_platform(pool, _fn)


async def list_pollable_tools(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    """Return every tool's (tool_id, name, tenant_id, base_url, last_etag) for the poller.

    Platform-scoped: the poller spans all tenants. The base_url is read from the latest
    active version's manifest (a ``base_url`` field) when present, else falls back to a
    conventional in-cluster name (handled by the caller). last_etag comes from
    tool_health.
    """

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT t.tool_id, t.name, t.tenant_id::text AS tenant_id,
                   tv.manifest, h.last_etag
              FROM tools t
              JOIN LATERAL (
                   SELECT manifest, created_at
                     FROM tool_versions
                    WHERE tool_id = t.tool_id AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
              ) tv ON TRUE
              LEFT JOIN tool_health h ON h.tool_id = t.tool_id
            """
        )
        return await cur.fetchall()

    return await in_platform(pool, _fn)


# ── Access control (Phase 5) ────────────────────────────────────────────────────
async def resolve_agent_tool_access(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    agent_id: str,
    tool_server_name: str,
    capability: str | None,
    is_restricted: bool,
    restricted_default: str = "none",
) -> str:
    """Resolve the effective access mode for (agent, tool server, capability).

    Precedence: an explicit row for the exact (server, capability) wins; else an explicit
    server-wide row (capability IS NULL); else the DEFAULT — the restricted tool's own
    ``restricted_default`` (``none`` unless the publisher chose ``ask``) for a restricted
    tool, ``automated`` otherwise.
    """

    async def _fn(conn: AsyncConnection) -> str:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT access_mode, tool_capability
              FROM tools.agent_tool_access
             WHERE agent_id = %s::uuid
               AND tool_server_name = %s
               AND (tool_capability = %s OR tool_capability IS NULL)
             ORDER BY (tool_capability IS NOT NULL) DESC   -- exact-capability row first
             LIMIT 1
            """,
            (agent_id, tool_server_name, capability),
        )
        row = await cur.fetchone()
        if row is not None:
            return str(row["access_mode"])
        return restricted_default if is_restricted else "automated"

    return await in_tenant(pool, tenant_id, _fn)


async def list_agent_tool_access(
    pool: AsyncConnectionPool, tenant_id: str, agent_id: str
) -> list[dict[str, Any]]:
    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT id::text, agent_id::text, tool_server_name, tool_capability,
                   access_mode, updated_at
              FROM tools.agent_tool_access
             WHERE agent_id = %s::uuid
             ORDER BY tool_server_name, tool_capability NULLS FIRST
            """,
            (agent_id,),
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def set_agent_tool_access(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    agent_id: str,
    tool_server_name: str,
    capability: str | None,
    access_mode: str,
) -> dict[str, Any]:
    """Upsert an agent's access mode for a tool server (+ optional capability)."""

    async def _fn(conn: AsyncConnection) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            INSERT INTO tools.agent_tool_access
              (tenant_id, agent_id, tool_server_name, tool_capability, access_mode)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s::uuid, %s, %s, %s)
            -- COALESCE so a server-wide rule (tool_capability IS NULL) has ONE canonical key;
            -- a plain (..., tool_capability) target never matches NULL=NULL and would duplicate.
            ON CONFLICT (tenant_id, agent_id, tool_server_name, COALESCE(tool_capability, '')) DO UPDATE
              SET access_mode = EXCLUDED.access_mode, updated_at = NOW()
            RETURNING id::text, agent_id::text, tool_server_name, tool_capability,
                      access_mode, updated_at
            """,
            (agent_id, tool_server_name, capability, access_mode),
        )
        row = await cur.fetchone()
        assert row is not None
        return row

    return await in_tenant(pool, tenant_id, _fn)


async def is_tool_restricted(pool: AsyncConnectionPool, tenant_id: str, tool_id: str) -> bool:
    async def _fn(conn: AsyncConnection) -> bool:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT 1 FROM tools.restricted_tools WHERE tool_id = %s", (tool_id,)
        )
        return (await cur.fetchone()) is not None

    return await in_tenant(pool, tenant_id, _fn)


async def get_restricted_default(
    pool: AsyncConnectionPool, tenant_id: str, tool_id: str
) -> str | None:
    """The tool's server-wide default access mode if it is restricted, else ``None``.

    ``None`` means the tool is not restricted (agents default to ``automated``). A returned
    string (``none``/``ask``/``automated``) is the fallback an agent gets when it has no
    explicit per-agent access row.
    """

    async def _fn(conn: AsyncConnection) -> str | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT default_access_mode FROM tools.restricted_tools WHERE tool_id = %s",
            (tool_id,),
        )
        row = await cur.fetchone()
        return str(row["default_access_mode"]) if row is not None else None

    return await in_tenant(pool, tenant_id, _fn)


async def list_restricted_tools(pool: AsyncConnectionPool, tenant_id: str) -> list[dict[str, Any]]:
    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT r.tool_id, t.name, r.tenant_id::text AS tenant_id, r.reason, r.created_at
              FROM tools.restricted_tools r
              JOIN tools.tools t ON t.tool_id = r.tool_id
             ORDER BY t.name
            """
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def mark_tool_restricted(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    tool_id: str,
    reason: str,
    default_access_mode: str = "none",
) -> None:
    async def _fn(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            INSERT INTO tools.restricted_tools (tool_id, tenant_id, reason, default_access_mode)
            VALUES (%s, NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s)
            -- DO NOTHING (not DO UPDATE): restricted_tools.tool_id is the PK, so an existing
            -- row may belong to ANOTHER tenant and be RLS-invisible — a DO UPDATE on it errors.
            -- Marking an already-restricted tool is idempotent.
            ON CONFLICT (tool_id) DO NOTHING
            """,
            (tool_id, reason, default_access_mode),
        )
        # Re-publish/edit path: refresh the reason + default mode for OUR OWN row. The RLS
        # write policy (WITH CHECK own tenant) + the explicit tenant predicate scope this to
        # the caller's row, so it never touches another tenant's (RLS-invisible) restriction.
        await conn.execute(
            """
            UPDATE tools.restricted_tools
               SET reason = %s, default_access_mode = %s
             WHERE tool_id = %s
               AND tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            """,
            (reason, default_access_mode, tool_id),
        )

    await in_tenant(pool, tenant_id, _fn)


# ── Platform seed ─────────────────────────────────────────────────────────────
async def seed_platform_tool(
    pool: AsyncConnectionPool,
    *,
    name: str,
    version: str,
    manifest: dict[str, Any],
    capabilities: list[tuple[str, str]],
) -> str:
    """Idempotently seed a PLATFORM tool (tenant_id IS NULL) + version + capabilities.

    Platform-scoped (empty GUC). Returns the tool_id. Safe to call on every boot — an
    existing platform tool of the same name is reused (no duplicate, capabilities
    refreshed to the seed manifest).
    """

    async def _fn(conn: AsyncConnection) -> str:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT tool_id FROM tools WHERE name = %s AND tenant_id IS NULL", (name,)
        )
        existing = await cur.fetchone()
        if existing is None:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                INSERT INTO tools (tenant_id, name, status, latest_version)
                VALUES (NULL, %s, 'active', %s)
                RETURNING tool_id
                """,
                (name, version),
            )
            row = await cur.fetchone()
            assert row is not None
            tool_id = row["tool_id"]
        else:
            tool_id = existing["tool_id"]
            await conn.execute(
                "UPDATE tools SET latest_version = %s, status = 'active' WHERE tool_id = %s",
                (version, tool_id),
            )

        # Version row (idempotent on (tool_id, version)).
        await conn.execute(
            """
            INSERT INTO tool_versions (tenant_id, tool_id, version, manifest, status)
            VALUES (NULL, %s, %s, %s, 'active')
            ON CONFLICT (tool_id, version) DO UPDATE SET manifest = EXCLUDED.manifest
            """,
            (tool_id, version, Jsonb(manifest)),
        )
        await conn.execute("DELETE FROM tool_capabilities WHERE tool_id = %s", (tool_id,))
        for capability, required_scope in capabilities:
            await conn.execute(
                """
                INSERT INTO tool_capabilities (tenant_id, tool_id, capability, required_scope)
                VALUES (NULL, %s, %s, %s)
                """,
                (tool_id, capability, required_scope),
            )
        await conn.execute(
            """
            INSERT INTO tool_health (tenant_id, tool_id, status, consecutive_failures)
            VALUES (NULL, %s, 'active', 0)
            ON CONFLICT (tool_id) DO NOTHING
            """,
            (tool_id,),
        )
        return str(tool_id)

    return await in_platform(pool, _fn)
