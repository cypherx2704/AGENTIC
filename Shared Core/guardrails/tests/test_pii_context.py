"""Unit tests for the B8 native context-window PII validation (default-off Tier 2)."""

from __future__ import annotations

from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import builtin_platform_default
from guardrails_service.services.rules.definitions import (
    RuleContext,
    detect_pii_name_context,
    detect_pii_passport_context,
)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KEY = "test-key"

_PASSPORT_TERMS = ("passport", "passport number", "document number")
_NAME_TERMS = ("my name is", "mr", "mrs", "dr", "prof")


def _ctx(enabled: bool = True, window: int = 40) -> RuleContext:
    return RuleContext(
        pii_context_enabled=enabled,
        pii_context_window=window,
        pii_context_passport_terms=_PASSPORT_TERMS,
        pii_context_name_terms=_NAME_TERMS,
    )


def test_default_off_is_inert() -> None:
    # Flag off (default) => both context detectors are a pure no-op (byte-identical path).
    assert detect_pii_passport_context("My passport number is X1234567.", RuleContext()) == []
    assert detect_pii_name_context("Contact Dr. Jane Smith.", RuleContext()) == []


def test_passport_admitted_with_context_term() -> None:
    hits = detect_pii_passport_context("My passport number is X1234567 issued in 2020.", _ctx())
    assert [h.matched_text for h in hits] == ["X1234567"]
    assert hits[0].category == "passport"


def test_passport_suppressed_without_context_term() -> None:
    # A bare alphanumeric with no supporting term must NOT be flagged (precision guard).
    assert detect_pii_passport_context("Order confirmation code AB123456 shipped.", _ctx()) == []


def test_passport_requires_a_digit() -> None:
    # A 6-9 char all-letter token is not a passport number even with context nearby.
    assert detect_pii_passport_context("passport ABCDEFG please", _ctx()) == []


def test_name_honorific_gated() -> None:
    hits = detect_pii_name_context("Please contact Dr. Jane Smith tomorrow.", _ctx())
    assert [h.matched_text for h in hits] == ["Jane Smith"]
    assert hits[0].category == "name"


def test_context_window_bounds_proximity() -> None:
    # The supporting term is far (> window) from the candidate -> not admitted.
    far = "passport " + ("x " * 60) + "X1234567"
    assert detect_pii_passport_context(far, _ctx(window=20)) == []


def test_context_passport_redacts_via_pipeline() -> None:
    result = evaluate(
        text="My passport number is X1234567.",
        policy=builtin_platform_default(),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=_ctx(),
    )
    assert result.decision == "redact"
    pt = result.processed_text or ""
    assert "X1234567" not in pt
    assert "[REDACTED:passport:" in pt
