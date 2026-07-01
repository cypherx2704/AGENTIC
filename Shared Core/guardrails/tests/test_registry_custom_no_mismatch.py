"""Readiness fix — a registered tenant CUSTOM rule must not flip /readyz to 'mismatch'.

The custom-rule loader REGISTERS tenant specs (tenant_id IS NOT NULL) into the shared
``RULES_BY_ID`` at request time. The overlay's mismatch check previously compared the live
(mutated) ``RULES_BY_ID`` against the platform-only DB rows (its ``_fetch`` filters
tenant_id IS NULL), so every loaded custom rule looked "missing_in_db" and spuriously failed
readiness. The check now compares the IMMUTABLE built-in platform set (``ALL_RULES``) instead.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from guardrails_service.services.rules import (
    ALL_RULES,
    RULES_BY_ID,
    RuleHit,
    RuleRegistryOverlay,
    RuleSpec,
)
from guardrails_service.services.rules import registry as registry_mod

Row = tuple[str, str, str, str, int, str]


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
    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows

    def connection(self, timeout: float | None = None) -> _FakeConnCtx:
        return _FakeConnCtx(_FakeConn(self.rows))


def _platform_rows() -> list[Row]:
    """The PLATFORM (tenant_id IS NULL) rows the overlay's _fetch returns — the built-in 11."""
    return [
        (s.rule_id, s.default_action, s.default_fail_mode, s.severity, s.timeout_ms, s.status)
        for s in ALL_RULES
    ]


@pytest.fixture(autouse=True)
def _drop_injected_customs() -> Iterator[None]:
    builtin = set(RULES_BY_ID)
    yield
    for rid in list(RULES_BY_ID):
        if rid not in builtin:
            del RULES_BY_ID[rid]


async def test_registered_custom_rule_does_not_cause_mismatch() -> None:
    # Simulate a tenant custom rule registered into the shared registry (what the loader does).
    RULES_BY_ID["custom-tenant-xyz:v1"] = RuleSpec(
        rule_id="custom-tenant-xyz:v1",
        name="Custom",
        direction="input",
        default_action="block",
        severity="high",
        category="security",
        detect=lambda _t, _c: [RuleHit("x", "security")],
        tags=["custom", "tenant:abc"],
    )

    overlay = RuleRegistryOverlay(FakeRulesPool(_platform_rows()))  # type: ignore[arg-type]
    status = await overlay.load_once()

    # The custom rule (not a platform row) must NOT be reported as missing_in_db.
    assert status == registry_mod.STATUS_OK
    assert overlay.missing_in_db == ()
    assert "custom-tenant-xyz:v1" not in overlay.missing_in_db
