"""MANDATORY cross-tenant-denial test (Contract 13) for the cypherx_a1 tenant-scoped tables.

DB-gated: runs only when ``CYPHERXA1_TEST_DSN`` points at a throwaway Postgres (an owner/
admin DSN — the init migration creates extensions + roles). The schema uses ENABLE + FORCE
RLS, so even the owner is subject to the policy: with ``app.tenant_id`` set to tenant A, only
A's rows are visible. Every NEW tenant-scoped table must be covered here (see CLAUDE.md)."""

from __future__ import annotations

import os
import pathlib

import pytest

DSN = os.environ.get("CYPHERXA1_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set CYPHERXA1_TEST_DSN (owner DSN) to run RLS tests")

_MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations" / "20260614_0001__init.sql"
TENANT_A = "00000000-0000-0000-0000-0000000000aa"
TENANT_B = "00000000-0000-0000-0000-0000000000bb"


async def _apply_migration(conn) -> None:  # noqa: ANN001
    await conn.execute(_MIGRATION.read_text(encoding="utf-8"))


async def test_cross_tenant_entities_are_invisible() -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await _apply_migration(conn)

        # Insert one entity per tenant, each inside its own tenant context (WITH CHECK passes).
        for tenant in (TENANT_A, TENANT_B):
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
                await conn.execute(
                    """INSERT INTO cypherx_a1.entities (tenant_id, kind, source, natural_key, title)
                       VALUES (%s::uuid, 'repo', 'test', %s, %s)
                       ON CONFLICT (tenant_id, kind, natural_key) WHERE valid_to IS NULL DO NOTHING""",
                    (tenant, f"repo-for-{tenant}", f"repo-{tenant}"),
                )

        # As tenant A, only A's row is visible (FORCE RLS applies even to the owner).
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (TENANT_A,))
            cur = await conn.execute("SELECT count(*) FROM cypherx_a1.entities")
            (visible,) = await cur.fetchone()
            assert visible == 1, f"tenant A must see exactly its own row, saw {visible}"

            cur = await conn.execute(
                "SELECT count(*) FROM cypherx_a1.entities WHERE tenant_id = %s::uuid", (TENANT_B,)
            )
            (leaked,) = await cur.fetchone()
            assert leaked == 0, "tenant A must NOT see tenant B's rows (cross-tenant denial)"
