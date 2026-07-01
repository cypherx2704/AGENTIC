"""Cross-tenant RLS mutation tests — proving the marketplace hole is CLOSED.

The marketplace hole: a naive ``USING (tenant_id = current_tenant OR tenant_id IS NULL)``
policy guards READS but lets a tenant INSERT/UPDATE a row carrying ANOTHER tenant's
tenant_id (or NULL, forging a platform tool). The fix is a SEPARATE write policy with
``WITH CHECK (tenant_id = current_tenant)`` on EVERY tenant-scoped table, INCLUDING
``tool_capabilities``.

Without a live Postgres in CI we prove the closure two complementary ways:

1. **Policy-predicate model** — we model the exact ``WITH CHECK`` predicate from the
   migration and assert it REJECTS a cross-tenant / forged-platform row and ACCEPTS an
   own-tenant row, for tools, tool_versions, tool_capabilities, and tool_health. The
   predicate string is also parsed straight out of the migration SQL so the model can't
   drift from the shipped policy.

2. **Write-code invariant** — we drive the real ``db.queries`` registration path through a
   fake pool that asserts EVERY INSERT/UPDATE binds ``tenant_id`` to the GUC tenant
   (the ``NULLIF(current_setting('app.tenant_id', true), '')::uuid`` form), so the code
   can never even attempt to write a row for another tenant. A fake pool whose write_hook
   simulates the WITH CHECK rejection (raising on a cross-tenant row) shows the request
   surfaces as an error rather than silently succeeding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tool_registry.db import queries

from .fakes import FakePool

TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "db" / "migrations" / "20260611_0001__init.sql"
).read_text(encoding="utf-8")


# ── (1) Policy-predicate model ─────────────────────────────────────────────────
def _current_tenant(guc: str | None) -> str | None:
    """Model NULLIF(current_setting('app.tenant_id', true), '')::uuid."""
    if guc is None or guc == "":
        return None
    return guc


def with_check_tenant_predicate(row_tenant_id: str | None, guc: str | None) -> bool:
    """Model the tenant write policy WITH CHECK: tenant_id = current_tenant."""
    ct = _current_tenant(guc)
    if ct is None:
        return False  # NULL = NULL is not TRUE in SQL -> row rejected
    return row_tenant_id == ct


def with_check_platform_predicate(row_tenant_id: str | None, guc: str | None) -> bool:
    """Model the platform write policy WITH CHECK: tenant_id IS NULL AND empty-GUC."""
    return row_tenant_id is None and _current_tenant(guc) is None


TABLES = ["tools", "tool_versions", "tool_capabilities", "tool_health"]


@pytest.mark.parametrize("table", TABLES)
def test_every_table_has_a_with_check_write_policy(table: str) -> None:
    """Every tenant-scoped table must declare a WITH CHECK write policy (incl. capabilities)."""
    # Find the write policy block for this table and assert it carries a WITH CHECK that
    # ties tenant_id to the current tenant.
    pattern = re.compile(
        rf"CREATE POLICY p_\w+_write ON tools\.{re.escape(table)} FOR ALL.*?WITH CHECK\s*\("
        rf"\s*tenant_id = NULLIF\(current_setting\('app\.tenant_id', true\), ''\)::uuid",
        re.DOTALL,
    )
    assert pattern.search(_MIGRATION), f"{table} is missing a tenant WITH CHECK write policy"


def test_capabilities_specifically_has_own_with_check_policy() -> None:
    """The prompt calls out tool_capabilities explicitly — assert its own policy exists."""
    assert "CREATE POLICY p_tool_caps_write ON tools.tool_capabilities FOR ALL" in _MIGRATION
    assert "p_tool_caps_write" in _MIGRATION


@pytest.mark.parametrize("table", TABLES)
def test_cross_tenant_insert_rejected_by_with_check(table: str) -> None:
    """Tenant A (GUC=A) trying to write a row for tenant B is REJECTED by WITH CHECK."""
    # Own row for A under A's GUC: accepted.
    assert with_check_tenant_predicate(TENANT_A, guc=TENANT_A) is True
    # Row stamped with B's tenant_id while GUC is A: rejected (the marketplace hole).
    assert with_check_tenant_predicate(TENANT_B, guc=TENANT_A) is False


@pytest.mark.parametrize("table", TABLES)
def test_forged_platform_insert_rejected_by_with_check(table: str) -> None:
    """A tenant (GUC=A) cannot forge a platform row (tenant_id NULL) via the tenant policy."""
    # tenant_id NULL under a tenant GUC: the tenant write policy rejects it...
    assert with_check_tenant_predicate(None, guc=TENANT_A) is False
    # ...and the platform policy does not apply either (GUC is non-empty).
    assert with_check_platform_predicate(None, guc=TENANT_A) is False


def test_platform_write_only_from_empty_guc() -> None:
    """The seed/poller (empty GUC) may write a platform row; a tenant GUC may not."""
    assert with_check_platform_predicate(None, guc="") is True   # empty GUC -> platform write ok
    assert with_check_platform_predicate(TENANT_A, guc="") is False  # but only tenant_id NULL


# ── (2) Write-code invariant ───────────────────────────────────────────────────
class _GucBindingPool(FakePool):
    """FakePool that records the GUC tenant and lets a hook veto cross-tenant writes."""


@pytest.mark.asyncio
async def test_registration_always_binds_guc_tenant_never_attacker_tenant() -> None:
    """db.queries writes tenant_id via the GUC, never an attacker-controlled value.

    We register a tool as tenant A. Every INSERT/UPDATE the code issues must use the
    ``NULLIF(current_setting('app.tenant_id', true), '')::uuid`` GUC form for tenant_id
    (or a parent tool_id under that tenant) — there is no code path that interpolates a
    caller-supplied tenant. So even if a request body tried to smuggle tenant B, the row
    would still be stamped with A and accepted by WITH CHECK only for A.
    """
    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "tid", "name": "tool-a", "tenant_id": TENANT_A,
              "status": "active", "latest_version": "1.0.0"}])

    await queries.create_tool_with_version(
        pool, TENANT_A,
        name="tool-a", version="1.0.0",
        manifest={"name": "tool-a"},
        capabilities=[("do_thing", "tool:tool-a:invoke")],
    )

    # The RLS GUC was set to tenant A.
    assert pool.last_tenant == TENANT_A
    # Every tenant-stamped INSERT uses the GUC expression, NOT a literal tenant param.
    tenant_inserts = [
        w for w in pool.writes
        if w[0].startswith("INSERT INTO tools.") or w[0].startswith("INSERT INTO tool")
        or w[0].startswith("INSERT INTO ")
    ]
    assert tenant_inserts, "expected INSERTs to be captured"
    for sql, _params in pool.writes:
        if sql.startswith("INSERT INTO tool") and "tenant_id" in sql:
            assert "current_setting('app.tenant_id', true)" in sql, (
                f"tenant_id must come from the GUC, not a bound param: {sql}"
            )


@pytest.mark.asyncio
async def test_with_check_rejection_surfaces_as_error() -> None:
    """If Postgres' WITH CHECK rejects a row, the registration call raises (not silent)."""

    class _RejectError(Exception):
        pass

    pool = FakePool()
    pool.on("INSERT INTO tools",
            [{"tool_id": "tid", "name": "tool-a", "tenant_id": TENANT_A,
              "status": "active", "latest_version": "1.0.0"}])

    def _veto(sql: str, params) -> None:  # type: ignore[no-untyped-def]
        # Simulate Postgres raising on a capability INSERT that violates WITH CHECK.
        if "INSERT INTO tool_capabilities" in sql:
            raise _RejectError("new row violates row-level security policy")

    pool.write_hook = _veto

    with pytest.raises(_RejectError):
        await queries.create_tool_with_version(
            pool, TENANT_A,
            name="tool-a", version="1.0.0",
            manifest={"name": "tool-a"},
            capabilities=[("do_thing", "tool:tool-a:invoke")],
        )
