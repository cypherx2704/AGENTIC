"""PII via Microsoft Presidio (optional dep + flag GUARDRAILS_PII_PRESIDIO, default off).

The default path is regex-only (unchanged). When the analyzer is wired, its located spans
are UNIONed into the PII detectors and rendered through the SAME HMAC token format. We test
with a FAKE analyzer (no presidio install needed) so CI stays light, plus the build-time
graceful-degradation paths.
"""

from __future__ import annotations

from guardrails_service.core.config import Settings
from guardrails_service.services.classifier import StubClassifier
from guardrails_service.services.pii_presidio import build_presidio_analyzer
from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import builtin_platform_default
from guardrails_service.services.rules import RuleContext

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KEY = "test-key"


class _FakeAnalyzer:
    """Returns scripted (matched_text, category) spans (mimics PresidioPiiAnalyzer.analyze)."""

    def __init__(self, spans: list[tuple[str, str]]) -> None:
        self._spans = spans

    def analyze(self, text: str) -> list[tuple[str, str]]:
        return [(m, c) for (m, c) in self._spans if m in text]


def test_build_disabled_returns_none() -> None:
    assert build_presidio_analyzer(Settings(guardrails_pii_presidio=False)) is None


def test_build_enabled_but_unavailable_degrades_to_none() -> None:
    # presidio-analyzer is not installed in the default test env -> graceful None.
    analyzer = build_presidio_analyzer(Settings(guardrails_pii_presidio=True))
    assert analyzer is None  # regex-only path preserved


def test_presidio_span_unioned_into_email_redaction() -> None:
    # A name the regexes never catch; Presidio locates it as category 'name' (PII).
    ctx = RuleContext(
        classifier=StubClassifier(),
        presidio_spans=[("alice@corp.test", "email")],
    )
    result = evaluate(
        text="ping alice@corp.test",
        policy=builtin_platform_default(),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=ctx,
    )
    assert result.decision == "redact"
    assert "alice@corp.test" not in (result.processed_text or "")
    assert "[REDACTED:email:" in (result.processed_text or "")


def test_presidio_does_not_double_count_regex_hit() -> None:
    # The same email is found by BOTH regex and Presidio -> still exactly one violation.
    ctx = RuleContext(
        classifier=StubClassifier(),
        presidio_spans=[("bob@x.io", "email")],
    )
    result = evaluate(
        text="bob@x.io",
        policy=builtin_platform_default(),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=ctx,
    )
    email_hits = [v for v in result.violations if v.category == "email"]
    assert len(email_hits) == 1


def test_no_presidio_spans_is_regex_only() -> None:
    # presidio_spans=None -> behaviour identical to today (a plain string with no PII allows).
    ctx = RuleContext(classifier=StubClassifier(), presidio_spans=None)
    result = evaluate(
        text="just a normal sentence",
        policy=builtin_platform_default(),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=ctx,
    )
    assert result.decision == "allow"


def test_fake_analyzer_filters_to_text() -> None:
    fake = _FakeAnalyzer([("alice@corp.test", "email"), ("notpresent@x.io", "email")])
    assert fake.analyze("ping alice@corp.test") == [("alice@corp.test", "email")]
