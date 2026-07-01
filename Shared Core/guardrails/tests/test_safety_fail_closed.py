"""Safety hardening — a timed-out SAFETY rule must NEVER silently allow (fail-CLOSED).

Research-aligned: the per-rule timeout budget is tight (10ms), so under load a safety rule
(e.g. ``output-pii-email-v1``) can overrun and — with a policy ``fail_mode_override='open'`` —
be SKIPPED (fail-open), letting PII pass unredacted. The pipeline now treats a timed-out/failed
SAFETY rule (category in the safety set) as a VIOLATION (block) regardless of fail_mode when the
caller opts in (``safety_fail_closed=True`` — the LIVE check path's default). NON-safety rules
(length) keep their fail_mode posture.

The detector RAISES to hit ``_run_detect``'s timeout-equivalent branch deterministically
(no wall-clock dependence), exactly like ``test_live_fail_mode``.
"""

from __future__ import annotations

import pytest

from guardrails_service.services.classifier import StubClassifier
from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import EffectivePolicy, EnabledRule
from guardrails_service.services.rules import RULES_BY_ID, RuleContext

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KEY = "test-key"


@pytest.fixture()
def failing_rule():  # type: ignore[no-untyped-def]
    """Make a chosen rule's detector FAIL (timeout-equivalent); restore after."""

    def _make(rule_id: str):  # type: ignore[no-untyped-def]
        spec = RULES_BY_ID[rule_id]
        orig = spec.detect

        def _boom(_text: str, _ctx: RuleContext):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated overrun")

        spec.detect = _boom  # type: ignore[assignment]
        return spec, orig

    made: list[tuple] = []

    def factory(rule_id: str) -> str:
        spec, orig = _make(rule_id)
        made.append((spec, orig))
        return rule_id

    yield factory
    for spec, orig in made:
        spec.detect = orig  # type: ignore[assignment]


def _policy(rule_id: str, fail_override: str | None) -> EffectivePolicy:
    return EffectivePolicy(
        policy_id="p", name="p",
        rules=(EnabledRule(rule_id),),
        fail_mode_override=fail_override,
    )


def _run(rule_id: str, direction: str, fail_override: str | None, *, safety_fail_closed: bool):
    return evaluate(
        text="anything",
        policy=_policy(rule_id, fail_override),
        direction=direction,
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=RuleContext(classifier=StubClassifier()),
        honor_fail_mode_override=True,
        safety_fail_closed=safety_fail_closed,
    )


def test_safety_rule_open_override_blocks_when_fail_closed_enabled(failing_rule) -> None:  # type: ignore[no-untyped-def]
    # pii-email-v1 is a SAFETY rule. Policy override 'open' would normally SKIP a timeout, but
    # with safety_fail_closed=True it is forced to block (PII must never pass on a timeout).
    rid = failing_rule("pii-email-v1")
    result = _run(rid, "input", "open", safety_fail_closed=True)
    assert result.decision == "block"
    assert any(v.rule_id == rid for v in result.violations)


def test_safety_rule_open_override_still_allows_when_fail_closed_disabled(failing_rule) -> None:  # type: ignore[no-untyped-def]
    # Default/opt-out posture (the prior behaviour): 'open' override skips the timed-out rule.
    rid = failing_rule("pii-email-v1")
    result = _run(rid, "input", "open", safety_fail_closed=False)
    assert result.decision == "allow"


def test_non_safety_rule_keeps_open_posture(failing_rule) -> None:  # type: ignore[no-untyped-def]
    # output-max-length-v1 is the only NON-safety rule (category 'length'); a timeout under an
    # 'open' override is still tolerated even with safety_fail_closed=True (no safety leak).
    rid = failing_rule("output-max-length-v1")
    result = _run(rid, "output", "open", safety_fail_closed=True)
    assert result.decision == "allow"


def test_safety_min_timeout_floor_raises_budget() -> None:
    # A safety rule whose detector sleeps ~12ms: with the rule's own 10ms budget it would time
    # out, but a 25ms safety floor keeps it under budget so it evaluates normally (here: no
    # email -> allow, NOT a fail-closed block).
    import time

    spec = RULES_BY_ID["pii-email-v1"]
    orig = spec.detect

    def _slow(_text: str, _ctx: RuleContext):  # type: ignore[no-untyped-def]
        time.sleep(0.012)
        return []

    spec.detect = _slow  # type: ignore[assignment]
    try:
        result = evaluate(
            text="no pii here",
            policy=_policy("pii-email-v1", "open"),
            direction="input",
            tenant_id=TENANT,
            redaction_key=KEY,
            ctx=RuleContext(classifier=StubClassifier()),
            honor_fail_mode_override=True,
            safety_fail_closed=True,
            safety_min_timeout_ms=25,
        )
        assert result.decision == "allow"
    finally:
        spec.detect = orig  # type: ignore[assignment]
