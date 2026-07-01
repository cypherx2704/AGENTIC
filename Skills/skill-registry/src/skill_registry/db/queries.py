"""Registry data-access: discovery, registration, version retention, health, seed.

All tenant-scoped reads/writes run inside :func:`skill_registry.db.pool.in_tenant`, so
``app.tenant_id`` is set for the transaction and RLS on the ``skills`` /
``skill_versions`` / ``skill_capabilities`` tables admits ONLY the caller's rows plus
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
_LIST_SKILLS_SQL = """
    SELECT t.skill_id, t.name, t.tenant_id::text AS tenant_id, t.status,
           t.latest_version, (t.tenant_id IS NULL) AS is_platform
      FROM skills t
     ORDER BY t.name, (t.tenant_id IS NULL)  -- tenant row (FALSE) sorts before platform (TRUE)
     LIMIT %s
"""


async def list_visible_skills(
    pool: AsyncConnectionPool, tenant_id: str, *, limit: int
) -> list[dict[str, Any]]:
    """Return skills visible to the tenant (own + platform), RLS-scoped.

    RLS admits the tenant's own rows AND platform rows (tenant_id IS NULL). The rows
    are ordered so that for a given name the tenant's row precedes the platform row;
    the API layer applies tenant-priority shadowing on top.
    """

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(_LIST_SKILLS_SQL, (limit,))
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def get_skill_rows_by_name(
    pool: AsyncConnectionPool, tenant_id: str, name: str
) -> list[dict[str, Any]]:
    """Return the skill row(s) for ``name`` visible to the tenant (own + platform)."""

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT skill_id, name, tenant_id::text AS tenant_id, status, latest_version,
                   (tenant_id IS NULL) AS is_platform
              FROM skills
             WHERE name = %s
            """,
            (name,),
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def get_version(
    pool: AsyncConnectionPool, tenant_id: str, skill_id: str, version: str | None
) -> dict[str, Any] | None:
    """Resolve a skill's version row: a specific ``version`` if given, else the latest active.

    Returns the version row (incl. resolved manifest JSONB) or ``None`` if no matching
    active version exists.
    """

    async def _fn(conn: AsyncConnection) -> dict[str, Any] | None:
        if version is not None:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT version, manifest, status, created_at
                  FROM skill_versions
                 WHERE skill_id = %s AND version = %s AND status = %s
                """,
                (skill_id, version, STATUS_ACTIVE),
            )
        else:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT version, manifest, status, created_at
                  FROM skill_versions
                 WHERE skill_id = %s AND status = %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (skill_id, STATUS_ACTIVE),
            )
        return await cur.fetchone()

    return await in_tenant(pool, tenant_id, _fn)


async def get_capabilities(
    pool: AsyncConnectionPool, tenant_id: str, skill_id: str
) -> list[dict[str, Any]]:
    """Return the declared capability/scope rows for a skill (RLS-scoped)."""

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT capability, required_scope
              FROM skill_capabilities
             WHERE skill_id = %s
             ORDER BY capability
            """,
            (skill_id,),
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def get_health(
    pool: AsyncConnectionPool, tenant_id: str, skill_id: str
) -> dict[str, Any] | None:
    """Return the ``skill_health`` row for a skill (RLS-scoped), or ``None``."""

    async def _fn(conn: AsyncConnection) -> dict[str, Any] | None:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT status, last_etag, consecutive_failures, last_polled
              FROM skill_health
             WHERE skill_id = %s
            """,
            (skill_id,),
        )
        return await cur.fetchone()

    return await in_tenant(pool, tenant_id, _fn)


# ── Registration ──────────────────────────────────────────────────────────────
async def create_skill_with_version(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    name: str,
    version: str,
    manifest: dict[str, Any],
    capabilities: list[tuple[str, str]],
) -> dict[str, Any]:
    """Register a NEW skill + its first version + capability rows in one transaction.

    All rows are written with ``tenant_id`` = the GUC tenant, so the WITH CHECK halves
    of the RLS policies accept them. A duplicate (tenant_id, name) raises a unique
    violation which the API maps to 409 CONFLICT.
    """

    async def _fn(conn: AsyncConnection) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            INSERT INTO skills (tenant_id, name, status, latest_version)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, 'active', %s)
            RETURNING skill_id, name, tenant_id::text AS tenant_id, status, latest_version
            """,
            (name, version),
        )
        skill = await cur.fetchone()
        assert skill is not None
        await _insert_version(conn, skill["skill_id"], version, manifest)
        await _replace_capabilities(conn, skill["skill_id"], capabilities)
        await _init_health(conn, skill["skill_id"])
        return skill

    return await in_tenant(pool, tenant_id, _fn)


async def add_version(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    skill_id: str,
    version: str,
    manifest: dict[str, Any],
    capabilities: list[tuple[str, str]],
    max_active_versions: int,
) -> dict[str, Any]:
    """Append a new active version to an existing skill, enforcing retention.

    After inserting the version we count active versions; if it exceeds
    ``max_active_versions`` we retire the OLDEST active version(s) down to the cap.
    ``latest_version`` on the parent skill is advanced and capabilities are refreshed
    from the new manifest. Returns ``{version, retired: [..]}``.
    """

    async def _fn(conn: AsyncConnection) -> dict[str, Any]:
        await _insert_version(conn, skill_id, version, manifest)
        await conn.execute(
            "UPDATE skills SET latest_version = %s WHERE skill_id = %s", (version, skill_id)
        )
        await _replace_capabilities(conn, skill_id, capabilities)
        retired = await _enforce_retention(conn, skill_id, max_active_versions)
        return {"version": version, "retired": retired}

    return await in_tenant(pool, tenant_id, _fn)


async def _insert_version(
    conn: AsyncConnection, skill_id: str, version: str, manifest: dict[str, Any]
) -> None:
    await conn.execute(
        """
        INSERT INTO skill_versions (tenant_id, skill_id, version, manifest, status)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, 'active')
        """,
        (skill_id, version, Jsonb(manifest)),
    )


async def _replace_capabilities(
    conn: AsyncConnection, skill_id: str, capabilities: list[tuple[str, str]]
) -> None:
    """Refresh the skill's capability rows to match the latest manifest."""
    await conn.execute("DELETE FROM skill_capabilities WHERE skill_id = %s", (skill_id,))
    for capability, required_scope in capabilities:
        await conn.execute(
            """
            INSERT INTO skill_capabilities (tenant_id, skill_id, capability, required_scope)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s)
            """,
            (skill_id, capability, required_scope),
        )


async def _init_health(conn: AsyncConnection, skill_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO skill_health (tenant_id, skill_id, status, consecutive_failures)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, 'active', 0)
        ON CONFLICT (skill_id) DO NOTHING
        """,
        (skill_id,),
    )


async def _enforce_retention(
    conn: AsyncConnection, skill_id: str, max_active_versions: int
) -> list[str]:
    """Retire the oldest active versions beyond ``max_active_versions``; return retired list."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT version
          FROM skill_versions
         WHERE skill_id = %s AND status = %s
         ORDER BY created_at DESC
        """,
        (skill_id, STATUS_ACTIVE),
    )
    active = [r["version"] for r in await cur.fetchall()]
    to_retire = active[max_active_versions:]  # everything past the newest N
    for version in to_retire:
        await conn.execute(
            "UPDATE skill_versions SET status = %s WHERE skill_id = %s AND version = %s",
            (STATUS_RETIRED, skill_id, version),
        )
    return to_retire


# ── Health persistence (platform-scoped: poller updates every skill) ────────────
async def update_health(
    pool: AsyncConnectionPool,
    *,
    skill_id: str,
    status: str,
    consecutive_failures: int,
    last_etag: str | None,
    manifest: dict[str, Any] | None,
) -> None:
    """Persist a health-poll outcome for a skill (platform-scoped UPSERT).

    Runs with an empty ``app.tenant_id`` (the poller spans all tenants). When the poll
    returned a changed manifest we also refresh the cached manifest on the latest
    active version row. skill_health rows are written without RLS gating on the GUC
    tenant because the table policy admits the poller (see migration).
    """

    async def _fn(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            INSERT INTO skill_health (skill_id, status, consecutive_failures, last_etag, last_polled)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (skill_id) DO UPDATE
              SET status = EXCLUDED.status,
                  consecutive_failures = EXCLUDED.consecutive_failures,
                  last_etag = COALESCE(EXCLUDED.last_etag, skill_health.last_etag),
                  last_polled = NOW()
            """,
            (skill_id, status, consecutive_failures, last_etag),
        )
        if manifest is not None:
            await conn.execute(
                """
                UPDATE skill_versions tv
                   SET manifest = %s
                 WHERE tv.skill_id = %s
                   AND tv.status = 'active'
                   AND tv.created_at = (
                       SELECT MAX(created_at) FROM skill_versions
                        WHERE skill_id = %s AND status = 'active'
                   )
                """,
                (Jsonb(manifest), skill_id, skill_id),
            )

    await in_platform(pool, _fn)


async def list_pollable_skills(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    """Return every skill's (skill_id, name, tenant_id, base_url, last_etag) for the poller.

    Platform-scoped: the poller spans all tenants. The base_url is read from the latest
    active version's manifest (a ``base_url`` field) when present, else falls back to a
    conventional in-cluster name (handled by the caller). last_etag comes from
    skill_health.
    """

    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT t.skill_id, t.name, t.tenant_id::text AS tenant_id,
                   tv.manifest, h.last_etag
              FROM skills t
              JOIN LATERAL (
                   SELECT manifest, created_at
                     FROM skill_versions
                    WHERE skill_id = t.skill_id AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
              ) tv ON TRUE
              LEFT JOIN skill_health h ON h.skill_id = t.skill_id
            """
        )
        return await cur.fetchall()

    return await in_platform(pool, _fn)


# ── Access control (Phase 5) ────────────────────────────────────────────────────
async def resolve_agent_skill_access(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    agent_id: str,
    skill_server_name: str,
    capability: str | None,
    is_restricted: bool,
) -> str:
    """Resolve the effective access mode for (agent, skill server, capability).

    Precedence: an explicit row for the exact (server, capability) wins; else an explicit
    server-wide row (capability IS NULL); else the DEFAULT — ``none`` for a restricted skill,
    ``automated`` otherwise.
    """

    async def _fn(conn: AsyncConnection) -> str:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT access_mode, skill_capability
              FROM skills.agent_skill_access
             WHERE agent_id = %s::uuid
               AND skill_server_name = %s
               AND (skill_capability = %s OR skill_capability IS NULL)
             ORDER BY (skill_capability IS NOT NULL) DESC   -- exact-capability row first
             LIMIT 1
            """,
            (agent_id, skill_server_name, capability),
        )
        row = await cur.fetchone()
        if row is not None:
            return str(row["access_mode"])
        return "none" if is_restricted else "automated"

    return await in_tenant(pool, tenant_id, _fn)


async def list_agent_skill_access(
    pool: AsyncConnectionPool, tenant_id: str, agent_id: str
) -> list[dict[str, Any]]:
    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT id::text, agent_id::text, skill_server_name, skill_capability,
                   access_mode, updated_at
              FROM skills.agent_skill_access
             WHERE agent_id = %s::uuid
             ORDER BY skill_server_name, skill_capability NULLS FIRST
            """,
            (agent_id,),
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def set_agent_skill_access(
    pool: AsyncConnectionPool,
    tenant_id: str,
    *,
    agent_id: str,
    skill_server_name: str,
    capability: str | None,
    access_mode: str,
) -> dict[str, Any]:
    """Upsert an agent's access mode for a skill server (+ optional capability)."""

    async def _fn(conn: AsyncConnection) -> dict[str, Any]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            INSERT INTO skills.agent_skill_access
              (tenant_id, agent_id, skill_server_name, skill_capability, access_mode)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s::uuid, %s, %s, %s)
            -- COALESCE so a server-wide rule (skill_capability IS NULL) has ONE canonical key;
            -- a plain (..., skill_capability) target never matches NULL=NULL and would duplicate.
            ON CONFLICT (tenant_id, agent_id, skill_server_name, COALESCE(skill_capability, '')) DO UPDATE
              SET access_mode = EXCLUDED.access_mode, updated_at = NOW()
            RETURNING id::text, agent_id::text, skill_server_name, skill_capability,
                      access_mode, updated_at
            """,
            (agent_id, skill_server_name, capability, access_mode),
        )
        row = await cur.fetchone()
        assert row is not None
        return row

    return await in_tenant(pool, tenant_id, _fn)


async def is_skill_restricted(pool: AsyncConnectionPool, tenant_id: str, skill_id: str) -> bool:
    async def _fn(conn: AsyncConnection) -> bool:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT 1 FROM skills.restricted_skills WHERE skill_id = %s", (skill_id,)
        )
        return (await cur.fetchone()) is not None

    return await in_tenant(pool, tenant_id, _fn)


async def list_restricted_skills(pool: AsyncConnectionPool, tenant_id: str) -> list[dict[str, Any]]:
    async def _fn(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = await conn.cursor(row_factory=dict_row).execute(
            """
            SELECT r.skill_id, t.name, r.tenant_id::text AS tenant_id, r.reason, r.created_at
              FROM skills.restricted_skills r
              JOIN skills.skills t ON t.skill_id = r.skill_id
             ORDER BY t.name
            """
        )
        return await cur.fetchall()

    return await in_tenant(pool, tenant_id, _fn)


async def mark_skill_restricted(
    pool: AsyncConnectionPool, tenant_id: str, *, skill_id: str, reason: str
) -> None:
    async def _fn(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            INSERT INTO skills.restricted_skills (skill_id, tenant_id, reason)
            VALUES (%s, NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s)
            -- DO NOTHING (not DO UPDATE): restricted_skills.skill_id is the PK, so an existing
            -- row may belong to ANOTHER tenant and be RLS-invisible — a DO UPDATE on it errors.
            -- Marking an already-restricted skill is idempotent.
            ON CONFLICT (skill_id) DO NOTHING
            """,
            (skill_id, reason),
        )

    await in_tenant(pool, tenant_id, _fn)


# ── Platform seed ─────────────────────────────────────────────────────────────
async def seed_platform_skill(
    pool: AsyncConnectionPool,
    *,
    name: str,
    version: str,
    manifest: dict[str, Any],
    capabilities: list[tuple[str, str]],
) -> str:
    """Idempotently seed a PLATFORM skill (tenant_id IS NULL) + version + capabilities.

    Platform-scoped (empty GUC). Returns the skill_id. Safe to call on every boot — an
    existing platform skill of the same name is reused (no duplicate, capabilities
    refreshed to the seed manifest).
    """

    async def _fn(conn: AsyncConnection) -> str:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT skill_id FROM skills WHERE name = %s AND tenant_id IS NULL", (name,)
        )
        existing = await cur.fetchone()
        if existing is None:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                INSERT INTO skills (tenant_id, name, status, latest_version)
                VALUES (NULL, %s, 'active', %s)
                RETURNING skill_id
                """,
                (name, version),
            )
            row = await cur.fetchone()
            assert row is not None
            skill_id = row["skill_id"]
        else:
            skill_id = existing["skill_id"]
            await conn.execute(
                "UPDATE skills SET latest_version = %s, status = 'active' WHERE skill_id = %s",
                (version, skill_id),
            )

        # Version row (idempotent on (skill_id, version)).
        await conn.execute(
            """
            INSERT INTO skill_versions (tenant_id, skill_id, version, manifest, status)
            VALUES (NULL, %s, %s, %s, 'active')
            ON CONFLICT (skill_id, version) DO UPDATE SET manifest = EXCLUDED.manifest
            """,
            (skill_id, version, Jsonb(manifest)),
        )
        await conn.execute("DELETE FROM skill_capabilities WHERE skill_id = %s", (skill_id,))
        for capability, required_scope in capabilities:
            await conn.execute(
                """
                INSERT INTO skill_capabilities (tenant_id, skill_id, capability, required_scope)
                VALUES (NULL, %s, %s, %s)
                """,
                (skill_id, capability, required_scope),
            )
        await conn.execute(
            """
            INSERT INTO skill_health (tenant_id, skill_id, status, consecutive_failures)
            VALUES (NULL, %s, 'active', 0)
            ON CONFLICT (skill_id) DO NOTHING
            """,
            (skill_id,),
        )
        return str(skill_id)

    return await in_platform(pool, _fn)
