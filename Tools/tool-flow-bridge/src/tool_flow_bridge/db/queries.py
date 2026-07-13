"""SQL data-access for the ``flow_tools`` schema (psycopg3 async, dict rows).

Every function takes an ``AsyncConnection`` already inside a tenant/platform transaction
(see ``db.pool.in_tenant`` / ``in_platform``); RLS enforces the tenant boundary. JSONB
columns are written via ``psycopg.types.json.Jsonb`` and read back as plain dicts.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# ── tenant_runtimes ───────────────────────────────────────────────────────────────


async def get_tenant_runtime(conn: AsyncConnection, tenant_id: str) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM flow_tools.tenant_runtimes WHERE tenant_id = %s", (tenant_id,)
        )
        return await cur.fetchone()


async def upsert_tenant_runtime(
    conn: AsyncConnection,
    tenant_id: str,
    *,
    internal_host: str,
    http_node_root: str,
    admin_token_ref: str,
    invoke_secret_ref: str,
    credential_secret_ref: str,
    status: str = "running",
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO flow_tools.tenant_runtimes
                (tenant_id, status, internal_host, http_node_root,
                 admin_token_ref, invoke_secret_ref, credential_secret_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                status = EXCLUDED.status,
                internal_host = EXCLUDED.internal_host,
                http_node_root = EXCLUDED.http_node_root,
                admin_token_ref = EXCLUDED.admin_token_ref,
                invoke_secret_ref = EXCLUDED.invoke_secret_ref,
                credential_secret_ref = EXCLUDED.credential_secret_ref,
                updated_at = NOW()
            RETURNING *
            """,
            (
                tenant_id,
                status,
                internal_host,
                http_node_root,
                admin_token_ref,
                invoke_secret_ref,
                credential_secret_ref,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def update_runtime_status(
    conn: AsyncConnection, runtime_id: str, status: str
) -> None:
    await conn.execute(
        "UPDATE flow_tools.tenant_runtimes SET status = %s, updated_at = NOW() "
        "WHERE runtime_id = %s",
        (status, runtime_id),
    )


# ── tool_bindings (DEPRECATED — read-only for rollback/history) ─────────────────────
# As of Phase 2 the publish/MCP-management path writes flow_tools.tools + flow_tools.mcps +
# flow_tools.mcp_tools as the source of truth (see below). ``tool_bindings`` is NO LONGER
# written by the publisher — ``create_binding`` / ``update_binding`` / ``set_binding_status`` /
# ``update_binding_access`` are retained ONLY for rollback + history of pre-Phase-2 rows and
# are not called by any live write path. Reads (``get_binding_*`` / ``list_bindings``) remain
# for legacy inspection. New writes go to ``create_tool`` / ``create_mcp`` / ``set_mcp_members``.


async def create_binding(
    conn: AsyncConnection,
    tenant_id: str,
    *,
    slug: str,
    snake_name: str,
    display_name: str,
    description: str,
    runtime_id: str,
    node_red_flow_id: str,
    http_method: str,
    http_path: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    manifest: dict[str, Any],
    version: str,
    access_mode: str,
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO flow_tools.tool_bindings
                (tenant_id, slug, snake_name, display_name, description, runtime_id,
                 node_red_flow_id, http_method, http_path, input_schema, output_schema,
                 manifest, version, access_mode, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            RETURNING *
            """,
            (
                tenant_id,
                slug,
                snake_name,
                display_name,
                description,
                runtime_id,
                node_red_flow_id,
                http_method,
                http_path,
                Jsonb(input_schema),
                Jsonb(output_schema) if output_schema is not None else None,
                Jsonb(manifest),
                version,
                access_mode,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def update_binding(
    conn: AsyncConnection,
    binding_id: str,
    *,
    snake_name: str,
    display_name: str,
    description: str,
    node_red_flow_id: str,
    http_method: str,
    http_path: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    manifest: dict[str, Any],
    version: str,
    access_mode: str,
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE flow_tools.tool_bindings SET
                snake_name = %s, display_name = %s, description = %s,
                node_red_flow_id = %s, http_method = %s, http_path = %s,
                input_schema = %s, output_schema = %s, manifest = %s,
                version = %s, access_mode = %s, status = 'active', updated_at = NOW()
            WHERE binding_id = %s
            RETURNING *
            """,
            (
                snake_name,
                display_name,
                description,
                node_red_flow_id,
                http_method,
                http_path,
                Jsonb(input_schema),
                Jsonb(output_schema) if output_schema is not None else None,
                Jsonb(manifest),
                version,
                access_mode,
                binding_id,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def get_binding_by_slug(conn: AsyncConnection, slug: str) -> dict[str, Any] | None:
    """Fetch a binding by slug (RLS scopes to the current tenant, or admits by slug in
    platform context via the empty-GUC read policy)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM flow_tools.tool_bindings WHERE slug = %s", (slug,)
        )
        return await cur.fetchone()


async def get_binding_with_runtime(
    conn: AsyncConnection, slug: str
) -> dict[str, Any] | None:
    """Binding joined to its tenant runtime — used by the invoke dispatch. RLS on both
    tables scopes the join to the caller's tenant."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT b.*, r.internal_host, r.http_node_root, r.invoke_secret_ref,
                   r.status AS runtime_status
            FROM flow_tools.tool_bindings b
            JOIN flow_tools.tenant_runtimes r ON r.runtime_id = b.runtime_id
            WHERE b.slug = %s
            """,
            (slug,),
        )
        return await cur.fetchone()


async def list_bindings(conn: AsyncConnection) -> list[dict[str, Any]]:
    """List the current tenant's bindings (RLS-scoped)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM flow_tools.tool_bindings ORDER BY updated_at DESC"
        )
        return list(await cur.fetchall())


async def set_binding_status(conn: AsyncConnection, binding_id: str, status: str) -> None:
    await conn.execute(
        "UPDATE flow_tools.tool_bindings SET status = %s, updated_at = NOW() "
        "WHERE binding_id = %s",
        (status, binding_id),
    )


async def update_binding_access(
    conn: AsyncConnection, binding_id: str, access_mode: str
) -> None:
    await conn.execute(
        "UPDATE flow_tools.tool_bindings SET access_mode = %s, updated_at = NOW() "
        "WHERE binding_id = %s",
        (access_mode, binding_id),
    )


# ── tools (atomic tool) ─────────────────────────────────────────────────────────────
# The `flow_tools.tool_bindings` queries above are SUPERSEDED by the tools/mcps/mcp_tools
# queries below (the atomic-tool + aggregating-MCP model). Bindings are retained for the
# legacy /w/<slug> wire + the current publish path until Phase 2 rewires writes here.


async def create_tool(
    conn: AsyncConnection,
    tenant_id: str,
    *,
    snake_name: str,
    display_name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    node_red_flow_id: str,
    http_method: str,
    http_path: str,
    runtime_id: str,
    version: str,
    access_mode: str,
    visibility: str = "private",
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO flow_tools.tools
                (tenant_id, snake_name, display_name, description, input_schema, output_schema,
                 node_red_flow_id, http_method, http_path, runtime_id, visibility, access_mode,
                 version, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            RETURNING *
            """,
            (
                tenant_id,
                snake_name,
                display_name,
                description,
                Jsonb(input_schema),
                Jsonb(output_schema) if output_schema is not None else None,
                node_red_flow_id,
                http_method,
                http_path,
                runtime_id,
                visibility,
                access_mode,
                version,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def update_tool(
    conn: AsyncConnection,
    tool_id: str,
    *,
    snake_name: str,
    display_name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    node_red_flow_id: str,
    http_method: str,
    http_path: str,
    version: str,
    access_mode: str,
    visibility: str,
) -> dict[str, Any]:
    """Update an atomic tool in place (re-publish). Re-activates it (status='active') so a
    previously-retired tool republished under the same slug becomes resolvable again."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE flow_tools.tools SET
                snake_name = %s, display_name = %s, description = %s,
                input_schema = %s, output_schema = %s, node_red_flow_id = %s,
                http_method = %s, http_path = %s, version = %s, access_mode = %s,
                visibility = %s, status = 'active', updated_at = NOW()
            WHERE tool_id = %s
            RETURNING *
            """,
            (
                snake_name,
                display_name,
                description,
                Jsonb(input_schema),
                Jsonb(output_schema) if output_schema is not None else None,
                node_red_flow_id,
                http_method,
                http_path,
                version,
                access_mode,
                visibility,
                tool_id,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def get_tool(conn: AsyncConnection, tool_id: str) -> dict[str, Any] | None:
    """Fetch one atomic tool by id (RLS scopes to the current tenant)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM flow_tools.tools WHERE tool_id = %s", (tool_id,))
        return await cur.fetchone()


async def get_tool_by_snake_name(
    conn: AsyncConnection, snake_name: str
) -> dict[str, Any] | None:
    """Most-recent atomic tool with this ``snake_name`` for the current tenant (RLS-scoped) —
    the create-vs-update discriminator on (re)publish (a tool's snake_name is its stable identity)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM flow_tools.tools WHERE snake_name = %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (snake_name,),
        )
        return await cur.fetchone()


async def list_tools(conn: AsyncConnection) -> list[dict[str, Any]]:
    """List the current tenant's ACTIVE atomic tools (RLS-scoped).

    Only ``status = 'active'`` rows are returned so the "Published tools" rail matches the
    active-only member semantics used everywhere else (``_load_mcp_members`` /
    ``get_mcp_with_members``): once a tool is unpublished (status -> 'retired') it drops off
    the listing instead of lingering as a stale published entry."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM flow_tools.tools WHERE status = 'active' ORDER BY updated_at DESC"
        )
        return list(await cur.fetchall())


async def owned_tool_ids(conn: AsyncConnection, tool_ids: list[str]) -> set[str]:
    """Return the subset of ``tool_ids`` that exist and are owned by the current tenant.

    RLS scopes the SELECT to the caller's tenant, so any id NOT returned is either non-existent
    or owned by another tenant — the app layer treats both as 'not owned' (finding #3)."""
    if not tool_ids:
        return set()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT tool_id FROM flow_tools.tools WHERE tool_id = ANY(%s)", (tool_ids,)
        )
        return {str(r["tool_id"]) for r in await cur.fetchall()}


async def set_tool_status(conn: AsyncConnection, tool_id: str, status: str) -> None:
    await conn.execute(
        "UPDATE flow_tools.tools SET status = %s, updated_at = NOW() WHERE tool_id = %s",
        (status, tool_id),
    )


async def update_tool_access(conn: AsyncConnection, tool_id: str, access_mode: str) -> None:
    await conn.execute(
        "UPDATE flow_tools.tools SET access_mode = %s, updated_at = NOW() WHERE tool_id = %s",
        (access_mode, tool_id),
    )


async def repoint_tool_runtime(
    conn: AsyncConnection, tool_id: str, *, runtime_id: str, node_red_flow_id: str
) -> None:
    """Re-home an atomic tool onto a different Node-RED runtime: point ``runtime_id`` at the target
    runtime and ``node_red_flow_id`` at the flow tab freshly created there. Used by
    ``publisher.promote_mcp`` after each member flow is copied into the platform runtime (the
    http-in URL path is preserved, so ``http_path`` is unchanged). RLS keeps the write tenant-owned;
    the ``runtime_id`` FK admits the platform (sentinel) runtime row."""
    await conn.execute(
        "UPDATE flow_tools.tools SET runtime_id = %s, node_red_flow_id = %s, updated_at = NOW() "
        "WHERE tool_id = %s",
        (runtime_id, node_red_flow_id, tool_id),
    )


async def set_tools_visibility(
    conn: AsyncConnection, tool_ids: list[str], visibility: str
) -> None:
    """Set ``visibility`` on a set of atomic tools in one statement. Used by
    ``publisher.promote_mcp`` to flip the promoted MCP's member tools to ``'public'`` in the SAME
    commit txn as the mcps rename/repoint, so the ``tools`` ``_public_read`` RLS policy
    (migration 0007, ``USING (visibility = 'public')``) admits them in a FOREIGN tenant's context —
    without this the cross-tenant ``/m/<slug>`` invoke resolves the public MCP but sees no members.
    The UPDATE runs in the owner's tenant context; RLS keeps the write own-tenant-only."""
    if not tool_ids:
        return
    await conn.execute(
        "UPDATE flow_tools.tools SET visibility = %s, updated_at = NOW() "
        "WHERE tool_id = ANY(%s)",
        (visibility, tool_ids),
    )


# ── mcps (the aggregating collection) ────────────────────────────────────────────────


async def create_mcp(
    conn: AsyncConnection,
    tenant_id: str,
    *,
    slug: str,
    server_name: str,
    display_name: str,
    description: str,
    visibility: str = "private",
    version: str = "1.0.0",
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO flow_tools.mcps
                (tenant_id, slug, server_name, display_name, description, visibility, version, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
            RETURNING *
            """,
            (tenant_id, slug, server_name, display_name, description, visibility, version),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def get_mcp_by_slug(conn: AsyncConnection, slug: str) -> dict[str, Any] | None:
    """Fetch an MCP by its globally-unique slug (RLS scopes to the current tenant, or admits by
    slug in platform context via the empty-GUC read policy — the unauth manifest poll)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM flow_tools.mcps WHERE slug = %s", (slug,))
        return await cur.fetchone()


async def get_mcp_by_id(conn: AsyncConnection, mcp_id: str) -> dict[str, Any] | None:
    """Fetch an MCP by id (RLS scopes to the current tenant)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM flow_tools.mcps WHERE mcp_id = %s", (mcp_id,))
        return await cur.fetchone()


async def list_mcps(conn: AsyncConnection) -> list[dict[str, Any]]:
    """List the current tenant's MCPs (RLS-scoped)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM flow_tools.mcps ORDER BY updated_at DESC")
        return list(await cur.fetchall())


async def _load_mcp_members(conn: AsyncConnection, mcp_id: str) -> list[dict[str, Any]]:
    """The ACTIVE member tools of an MCP, each joined to its tenant runtime (internal_host /
    http_node_root / invoke_secret_ref) for the governed invoke path. LEFT JOIN so the
    UNAUTHENTICATED manifest poll (platform context) still returns members even though
    tenant_runtimes has no platform RLS policy (the manifest needs no runtime fields)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT t.*, r.internal_host, r.http_node_root, r.invoke_secret_ref,
                   r.status AS runtime_status
            FROM flow_tools.mcp_tools mt
            JOIN flow_tools.tools t ON t.tool_id = mt.tool_id
            LEFT JOIN flow_tools.tenant_runtimes r ON r.runtime_id = t.runtime_id
            WHERE mt.mcp_id = %s AND t.status = 'active'
            ORDER BY t.snake_name
            """,
            (mcp_id,),
        )
        return list(await cur.fetchall())


async def get_mcp_with_members(
    conn: AsyncConnection, slug: str
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Resolve an MCP by slug plus its ACTIVE member tools (joined to their tenant runtime).

    Returns ``(mcp_row, member_rows)`` or ``None`` when no MCP has that slug. Both tables' RLS
    scopes the result to the caller's tenant (or admits by slug in platform context)."""
    mcp = await get_mcp_by_slug(conn, slug)
    if mcp is None:
        return None
    return mcp, await _load_mcp_members(conn, str(mcp["mcp_id"]))


async def get_mcp_members(conn: AsyncConnection, mcp_id: str) -> list[dict[str, Any]]:
    """The ACTIVE member tools (with runtime join) of an MCP by id — used when (re)registering the
    aggregating manifest after a membership change."""
    return await _load_mcp_members(conn, mcp_id)


async def get_member_tool_ids(conn: AsyncConnection, mcp_id: str) -> list[str]:
    """All member tool ids of an MCP (any status) — used to resolve a singleton's one tool on
    re-publish and to decide exclusive ownership on retire."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT tool_id FROM flow_tools.mcp_tools WHERE mcp_id = %s", (mcp_id,)
        )
        return [str(r["tool_id"]) for r in await cur.fetchall()]


async def exclusive_member_tool_ids(conn: AsyncConnection, mcp_id: str) -> list[str]:
    """Member tool ids reachable through ONLY this MCP — i.e. not exposed by any other ACTIVE MCP.

    Retiring an MCP must retire the tools it exclusively exposes (finding #4 case c) but MUST NOT
    retire a tool still reachable through a different MCP (many-to-many). "Reachable" means an
    ACTIVE sibling MCP: a membership in an already-RETIRED MCP does not keep a tool alive (that MCP
    no longer surfaces it), so the sibling test joins flow_tools.mcps and requires status='active'.
    Without this, unpublishing a tool's last ACTIVE MCP would orphan the tool as status='active'
    while no MCP exposes it."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT mt.tool_id FROM flow_tools.mcp_tools mt
            WHERE mt.mcp_id = %s
              AND NOT EXISTS (
                SELECT 1 FROM flow_tools.mcp_tools o
                  JOIN flow_tools.mcps m ON m.mcp_id = o.mcp_id
                 WHERE o.tool_id = mt.tool_id AND o.mcp_id <> %s AND m.status = 'active'
              )
            """,
            (mcp_id, mcp_id),
        )
        return [str(r["tool_id"]) for r in await cur.fetchall()]


async def list_tool_memberships(conn: AsyncConnection) -> list[dict[str, Any]]:
    """(tool_id, mcp_id, mcp_slug, mcp_server_name, mcp_status) rows for every membership of the
    current tenant — the join behind GET /v1/tools' per-tool MCP membership list (RLS-scoped)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT mt.tool_id, m.mcp_id, m.slug AS mcp_slug, m.server_name AS mcp_server_name,
                   m.status AS mcp_status
            FROM flow_tools.mcp_tools mt
            JOIN flow_tools.mcps m ON m.mcp_id = mt.mcp_id
            """
        )
        return list(await cur.fetchall())


async def update_mcp(
    conn: AsyncConnection,
    mcp_id: str,
    *,
    display_name: str,
    description: str,
    visibility: str,
    version: str,
) -> dict[str, Any]:
    """Update an MCP's metadata + version in place (PUT /v1/mcps/{id})."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE flow_tools.mcps SET
                display_name = %s, description = %s, visibility = %s, version = %s,
                status = 'active', updated_at = NOW()
            WHERE mcp_id = %s
            RETURNING *
            """,
            (display_name, description, visibility, version, mcp_id),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def promote_mcp_row(
    conn: AsyncConnection,
    mcp_id: str,
    *,
    slug: str,
    server_name: str,
    visibility: str,
    version: str,
) -> dict[str, Any]:
    """Re-point an MCP to the platform (public) namespace: new globally-unique slug/server_name +
    visibility='public' + bumped version (POST /v1/mcps/{id}/promote). Committed by
    ``publisher.promote_mcp`` (Phase 5) in the SAME transaction as the member tools' runtime repoint
    AND their visibility flip to 'public' (``set_tools_visibility``), AFTER the platform registration
    succeeds. The tenant_id column stays (flow_tools.mcps.tenant_id is NOT NULL) — visibility='public'
    is the public flag; the registry holds the tenant_id-NULL public row. Setting the mcps row (and
    its member tools) to visibility='public' is what the cross-tenant ``_public_read`` RLS policies
    (migration 0007) key on, so a FOREIGN tenant's ``/m/<slug>`` resolve of the public MCP + members
    succeeds; the member flows are re-homed onto the platform runtime (see publisher.promote_mcp),
    whose sentinel tenant_runtimes row is already readable in any context (migration 0006)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE flow_tools.mcps SET
                slug = %s, server_name = %s, visibility = %s, version = %s,
                status = 'active', updated_at = NOW()
            WHERE mcp_id = %s
            RETURNING *
            """,
            (slug, server_name, visibility, version, mcp_id),
        )
        row = await cur.fetchone()
        assert row is not None
        return row


async def set_mcp_status(conn: AsyncConnection, mcp_id: str, status: str) -> None:
    await conn.execute(
        "UPDATE flow_tools.mcps SET status = %s, updated_at = NOW() WHERE mcp_id = %s",
        (status, mcp_id),
    )


async def set_mcp_members(
    conn: AsyncConnection, mcp_id: str, tenant_id: str, tool_ids: list[str]
) -> None:
    """Replace an MCP's membership set with ``tool_ids`` (idempotent). Removes links no longer
    present, then inserts the new ones (ON CONFLICT DO NOTHING). RLS keeps every write tenant-owned."""
    async with conn.cursor() as cur:
        if tool_ids:
            await cur.execute(
                "DELETE FROM flow_tools.mcp_tools WHERE mcp_id = %s AND NOT (tool_id = ANY(%s))",
                (mcp_id, tool_ids),
            )
            await cur.executemany(
                """
                INSERT INTO flow_tools.mcp_tools (mcp_id, tool_id, tenant_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (mcp_id, tool_id) DO NOTHING
                """,
                [(mcp_id, tid, tenant_id) for tid in tool_ids],
            )
        else:
            await cur.execute("DELETE FROM flow_tools.mcp_tools WHERE mcp_id = %s", (mcp_id,))


async def add_mcp_member(
    conn: AsyncConnection, mcp_id: str, tool_id: str, tenant_id: str
) -> None:
    """Add one tool to an MCP's membership WITHOUT disturbing the rest (idempotent). Used when a
    freshly-created tool is attached to caller-specified MCPs (POST /v1/tools with mcp_ids)."""
    await conn.execute(
        """
        INSERT INTO flow_tools.mcp_tools (mcp_id, tool_id, tenant_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (mcp_id, tool_id) DO NOTHING
        """,
        (mcp_id, tool_id, tenant_id),
    )
