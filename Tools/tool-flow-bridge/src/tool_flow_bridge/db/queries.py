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


# ── tool_bindings ─────────────────────────────────────────────────────────────────


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
