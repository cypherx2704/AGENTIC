"""FIX 5 — the LIVE check path now honors policy.fail_mode_override (was simulation-only).

We use a rule whose detector deliberately FAILS so the fail-mode branch is exercised:
* fail_mode_override='open'  -> the failed rule is skipped (no block from the failure).
* fail_mode_override='closed' / honor disabled -> the rule's own default_fail_mode applies.

The detector RAISES (rather than sleeping past a timeout) so the fail-mode path is hit
DETERMINISTICALLY, independent of wall-clock resolution / machine load: ``_run_detect``
treats any detector exception as a timeout-equivalent failure (returns ``timed_out=True``),
which is exactly the branch ``fail_mode`` governs.
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
def slow_rule() -> str:
    """Temporarily make prompt-injection-v1 a detector that FAILS (timeout-equivalent)."""
    spec = RULES_BY_ID["prompt-injection-v1"]
    orig_detect = spec.detect
    orig_fail = spec.default_fail_mode

    def _boom(_text: str, _ctx: RuleContext):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated rule failure -> fail-mode branch")

    spec.detect = _boom  # type: ignore[assignment]
    spec.default_fail_mode = "closed"
    try:
        yield "prompt-injection-v1"
    finally:
        spec.detect = orig_detect  # type: ignore[assignment]
        spec.default_fail_mode = orig_fail


def _policy(fail_override: str | None) -> EffectivePolicy:
    return EffectivePolicy(
        policy_id="p", name="p",
        rules=(EnabledRule("prompt-injection-v1"),),
        fail_mode_override=fail_override,
    )


def test_live_honors_fail_mode_override_open(slow_rule: str) -> None:
    # default_fail_mode is 'closed' (would block on timeout), but the policy overrides to
    # 'open' -> with honor_fail_mode_override=True (live default) the timeout is tolerated.
    result = evaluate(
        text="anything",
        policy=_policy("open"),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=RuleContext(classifier=StubClassifier()),
        honor_fail_mode_override=True,
    )
    assert result.decision == "allow"


def test_live_default_fail_mode_when_override_absent(slow_rule: str) -> None:
    # No override + closed default -> timeout blocks.
    result = evaluate(
        text="anything",
        policy=_policy(None),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=RuleContext(classifier=StubClassifier()),
        honor_fail_mode_override=True,
    )
    assert result.decision == "block"


def test_disabling_honor_reverts_to_rule_default(slow_rule: str) -> None:
    # honor_fail_mode_override=False -> the 'open' override is ignored; closed default blocks.
    result = evaluate(
        text="anything",
        policy=_policy("open"),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=RuleContext(classifier=StubClassifier()),
        honor_fail_mode_override=False,
    )
    assert result.decision == "block"
