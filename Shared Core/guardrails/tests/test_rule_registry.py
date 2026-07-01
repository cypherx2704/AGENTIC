"""DB rule-metadata overlay (WP02): overlay application, mismatch, retired, refresh.

The overlay's DB read goes through the same ``pool.connection() -> conn.cursor(...)
.execute(...) -> fetchall()`` seam as the policy engine, so a tiny fake pool stands in
for psycopg — no real DB needed. The overlay MUTATES the module-global RuleSpec
objects (that is its job), so an autouse fixture snapshots + restores the metadata
around every test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from guardrails_service.services.classifier import StubClassifier
from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import builtin_platform_default
from guardrails_service.services.rules import RULES_BY_ID, RuleContext, RuleRegistryOverlay
from guardrails_service.services.rules import registry as registry_mod

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

Row = tuple[str, str, str, str, int, str]


# ── Fakes for the psycopg read seam ───────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows

    async def execute(self, query: str, params: object = None) -> _FakeCursor:
        return self

    async def fetchall(self) -> list[Row]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows

    def cursor(self, row_factory: object = None) -> _FakeCursor:
        return _FakeCursor(self._rows)


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeRulesPool:
    """Quacks like AsyncConnectionPool for the overlay's read path; rows are mutable."""

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.connections = 0

    def connection(self, timeout: float | None = None) -> _FakeConnCtx:
        self.connections += 1
        return _FakeConnCtx(_FakeConn(self.rows))


class FailingPool:
    """A pool whose connection acquisition always fails (DB down)."""

    def connection(self, timeout: float | None = None) -> _FakeConnCtx:
        raise RuntimeError("db down")


def _db_rows(overrides: dict[str, Row] | None = None) -> list[Row]:
    """Rows mirroring the current in-code metadata (the seed), with per-rule overrides."""
    overrides = overrides or {}
    rows: list[Row] = []
    for rule_id, spec in RULES_BY_ID.items():
        row: Row = (
            rule_id,
            spec.default_action,
            spec.default_fail_mode,
            spec.severity,
            spec.timeout_ms,
            spec.status,
        )
        rows.append(overrides.get(rule_id, row))
    return rows


@pytest.fixture(autouse=True)
def _restore_rule_specs() -> Iterator[None]:
    """Snapshot + restore the mutable RuleSpec metadata (the overlay mutates globals)."""
    snapshot = {
        rule_id: (spec.default_action, spec.default_fail_mode, spec.severity, spec.timeout_ms, spec.status)
        for rule_id, spec in RULES_BY_ID.items()
    }
    yield
    for rule_id, (action, fail_mode, severity, timeout_ms, status) in snapshot.items():
        spec = RULES_BY_ID[rule_id]
        spec.default_action = action
        spec.default_fail_mode = fail_mode
        spec.severity = severity
        spec.timeout_ms = timeout_ms
        spec.status = status


# ── Tests ─────────────────────────────────────────────────────────────────────────


async def test_no_pool_reports_ok() -> None:
    overlay = RuleRegistryOverlay(None)
    assert await overlay.load_once() == registry_mod.STATUS_OK


async def test_overlay_applies_db_metadata() -> None:
    rows = _db_rows({"pii-email-v1": ("pii-email-v1", "block", "open", "high", 25, "active")})
    overlay = RuleRegistryOverlay(FakeRulesPool(rows))  # type: ignore[arg-type]

    assert await overlay.load_once() == registry_mod.STATUS_OK

    spec = RULES_BY_ID["pii-email-v1"]
    assert spec.default_action == "block"
    assert spec.default_fail_mode == "open"
    assert spec.severity == "high"
    assert spec.timeout_ms == 25
    assert spec.status == "active"
    # An untouched rule keeps its (identical) metadata.
    assert RULES_BY_ID["jailbreak-v1"].default_action == "block"


async def test_code_rule_missing_from_db_is_mismatch() -> None:
    rows = [r for r in _db_rows() if r[0] != "jailbreak-v1"]
    overlay = RuleRegistryOverlay(FakeRulesPool(rows))  # type: ignore[arg-type]

    assert await overlay.load_once() == registry_mod.STATUS_MISMATCH
    assert overlay.missing_in_db == ("jailbreak-v1",)
    assert overlay.unknown_in_db == ()


async def test_unknown_db_rule_is_mismatch() -> None:
    rows = [*_db_rows(), ("pii-ssn-v9", "block", "closed", "high", 10, "active")]
    overlay = RuleRegistryOverlay(FakeRulesPool(rows))  # type: ignore[arg-type]

    assert await overlay.load_once() == registry_mod.STATUS_MISMATCH
    assert overlay.unknown_in_db == ("pii-ssn-v9",)
    assert overlay.missing_in_db == ()


async def test_db_unreachable_is_unavailable_and_code_defaults_stand() -> None:
    before = RULES_BY_ID["pii-email-v1"].default_action
    overlay = RuleRegistryOverlay(FailingPool())  # type: ignore[arg-type]

    assert await overlay.load_once() == registry_mod.STATUS_UNAVAILABLE
    assert RULES_BY_ID["pii-email-v1"].default_action == before


async def test_retired_rule_is_skipped_by_pipeline() -> None:
    rows = _db_rows(
        {"pii-email-v1": ("pii-email-v1", "redact", "closed", "medium", 10, "retired")}
    )
    overlay = RuleRegistryOverlay(FakeRulesPool(rows))  # type: ignore[arg-type]
    assert await overlay.load_once() == registry_mod.STATUS_OK

    result = evaluate(
        text="Email me at alice@example.com",
        policy=builtin_platform_default(),
        direction="input",
        tenant_id=TENANT,
        redaction_key="test-key",
        ctx=RuleContext(classifier=StubClassifier()),
    )
    # The only rule that fires on this text is retired -> nothing fires.
    assert result.decision == "allow"
    assert result.violations == []


async def test_refresh_loop_reapplies_metadata() -> None:
    pool = FakeRulesPool(_db_rows())
    overlay = RuleRegistryOverlay(pool, refresh_interval_seconds=0.01)  # type: ignore[arg-type]
    await overlay.load_once()
    assert RULES_BY_ID["pii-email-v1"].timeout_ms != 99

    await overlay.start()
    try:
        pool.rows = _db_rows(
            {"pii-email-v1": ("pii-email-v1", "redact", "closed", "medium", 99, "active")}
        )
        for _ in range(200):  # bounded wait (~2s) for the refresh tick to re-apply
            if RULES_BY_ID["pii-email-v1"].timeout_ms == 99:
                break
            await asyncio.sleep(0.01)
    finally:
        await overlay.stop()
    assert RULES_BY_ID["pii-email-v1"].timeout_ms == 99
