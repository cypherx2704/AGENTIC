"""Static assertions on the Phase-2 integrity migration (20260712_0005__mcp_integrity.sql).

There is no live Postgres in the unit suite, so the mcp_tools RLS backstop and the status-domain
CHECKs (findings #3 + #5) are verified by asserting the migration DDL declares them — the storage
layer that backstops the app-layer ownership validation and the 'active'|'retired' domain."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "20260712_0005__mcp_integrity.sql"
)
PLATFORM_RUNTIME_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "20260712_0006__platform_runtime.sql"
)
PUBLIC_READ_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "20260712_0007__public_mcp_read.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_exists() -> None:
    assert MIGRATION.is_file()


def test_status_check_on_tools_and_mcps() -> None:
    sql = _sql()
    # finding #5 — status domain CHECK on both new tables.
    assert re.search(r"flow_tools\.tools\b.*ADD CONSTRAINT tools_status_chk", sql, re.S)
    assert re.search(r"flow_tools\.mcps\b.*ADD CONSTRAINT mcps_status_chk", sql, re.S)
    assert sql.count("status IN ('active', 'retired')") >= 2


def test_mcp_tools_write_check_requires_owned_tool_and_mcp() -> None:
    sql = _sql()
    # finding #3 — the strengthened WITH CHECK requires a tenant-owned tools row AND mcps row,
    # not merely the denormalized mcp_tools.tenant_id.
    assert "CREATE POLICY p_mcp_tools_write ON flow_tools.mcp_tools" in sql
    assert re.search(r"WITH CHECK.*EXISTS.*flow_tools\.tools.*EXISTS.*flow_tools\.mcps", sql, re.S)
    assert "t.tool_id = mcp_tools.tool_id" in sql
    assert "m.mcp_id = mcp_tools.mcp_id" in sql


# ── Phase 5 · 5-bridge — platform runtime migration (20260712_0006) ───────────────
def _platform_sql() -> str:
    return PLATFORM_RUNTIME_MIGRATION.read_text(encoding="utf-8")


def test_platform_runtime_migration_exists() -> None:
    assert PLATFORM_RUNTIME_MIGRATION.is_file()


SENTINEL = "00000000-0000-0000-0000-000000000000"


def test_platform_runtime_read_policy_any_context_sentinel_only() -> None:
    """The sentinel (nil-UUID) platform runtime row is READABLE in every context (shared public
    infra) — a SELECT policy keyed ONLY to the sentinel tenant_id, never a real tenant's runtime."""
    sql = _platform_sql()
    assert "CREATE POLICY p_tenant_runtimes_platform_read ON flow_tools.tenant_runtimes FOR SELECT" in sql
    assert re.search(
        rf"p_tenant_runtimes_platform_read.*USING \(tenant_id = '{SENTINEL}'::uuid\)", sql, re.S
    )


def test_platform_runtime_write_policy_platform_context_only() -> None:
    """Writing (provision/upsert) the sentinel row is admitted ONLY in platform (empty-GUC) context
    AND only for the sentinel tenant_id — no tenant can write it, platform can't write real rows."""
    sql = _platform_sql()
    assert "CREATE POLICY p_tenant_runtimes_platform_write ON flow_tools.tenant_runtimes FOR ALL" in sql
    assert sql.count("NULLIF(current_setting('app.tenant_id', true), '') IS NULL") >= 2
    # Both USING and WITH CHECK pin the sentinel tenant_id.
    assert sql.count(f"tenant_id = '{SENTINEL}'::uuid") >= 3


# ── Phase 5 · cross-tenant Public-read migration (20260712_0007) ──────────────────
def _public_read_sql() -> str:
    return PUBLIC_READ_MIGRATION.read_text(encoding="utf-8")


def test_public_read_migration_exists() -> None:
    assert PUBLIC_READ_MIGRATION.is_file()


def test_public_read_policies_on_mcps_and_tools() -> None:
    """mcps + tools each get an ADDITIVE SELECT policy admitting a PUBLIC row in ANY tenant context
    (visibility is the public-invoke read boundary; USING (visibility = 'public'))."""
    sql = _public_read_sql()
    assert "CREATE POLICY p_mcps_public_read ON flow_tools.mcps FOR SELECT" in sql
    assert "CREATE POLICY p_tools_public_read ON flow_tools.tools FOR SELECT" in sql
    # Each new SELECT policy keys ONLY on visibility='public'.
    assert re.search(
        r"p_mcps_public_read ON flow_tools\.mcps FOR SELECT\s+USING \(visibility = 'public'\)", sql
    )
    assert re.search(
        r"p_tools_public_read ON flow_tools\.tools FOR SELECT\s+USING \(visibility = 'public'\)", sql
    )


def test_public_read_policy_on_mcp_tools_joins_public_mcp() -> None:
    """The mcp_tools link row is readable cross-tenant iff its MCP is PUBLIC (EXISTS-join on mcps)."""
    sql = _public_read_sql()
    assert "CREATE POLICY p_mcp_tools_public_read ON flow_tools.mcp_tools FOR SELECT" in sql
    assert re.search(
        r"p_mcp_tools_public_read.*EXISTS.*flow_tools\.mcps m", sql, re.S
    )
    assert "m.mcp_id = mcp_tools.mcp_id" in sql
    assert "m.visibility = 'public'" in sql


def test_public_read_policies_are_additive_select_only() -> None:
    """The widening is READ-only + additive: three FOR SELECT public-read policies, and the migration
    does NOT touch any _write policy (writes stay own-tenant-only)."""
    sql = _public_read_sql()
    assert sql.count("CREATE POLICY ") == 3
    assert sql.count("FOR SELECT") == 3
    # Every policy created here is SELECT-only — no write policy is (re)created (no FOR ALL / WITH
    # CHECK in the DDL), so the own-only _write policies from 0004/0005 stay untouched.
    assert "FOR ALL" not in sql
    assert "WITH CHECK (" not in sql
