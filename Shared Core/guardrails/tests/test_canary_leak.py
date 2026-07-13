"""Unit tests for the B7 per-request canary-token leak detector (default-off Tier 2)."""

from __future__ import annotations

import base64

from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import builtin_platform_default
from guardrails_service.services.rules.definitions import (
    RuleContext,
    detect_output_canary_leak,
)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KEY = "test-key"
TOKEN = "CANARY-9f3a1b7c2d4e"


def test_inert_without_tokens_byte_identical() -> None:
    # Field absent => inert (default path byte-identical), even if the token text appears.
    assert detect_output_canary_leak(f"echo {TOKEN}", RuleContext()) == []
    assert detect_output_canary_leak(f"echo {TOKEN}", RuleContext(canary_tokens=[])) == []


def test_exact_match_blocks() -> None:
    hits = detect_output_canary_leak(f"leaked: {TOKEN}", RuleContext(canary_tokens=[TOKEN]))
    assert len(hits) == 1


def test_despaced_hex_and_base64_variants() -> None:
    ctx = RuleContext(canary_tokens=[TOKEN])
    spaced = " ".join(TOKEN)
    assert detect_output_canary_leak(spaced, ctx)
    assert detect_output_canary_leak("blob " + TOKEN.encode().hex(), ctx)
    assert detect_output_canary_leak("b64 " + base64.b64encode(TOKEN.encode()).decode(), ctx)


def test_no_leak_is_clean() -> None:
    assert detect_output_canary_leak("nothing sensitive here", RuleContext(canary_tokens=[TOKEN])) == []


def test_matched_value_is_safe_no_raw_token() -> None:
    hits = detect_output_canary_leak(TOKEN, RuleContext(canary_tokens=[TOKEN]))
    assert hits and TOKEN not in hits[0].matched_text


def test_canary_leak_blocks_via_pipeline() -> None:
    result = evaluate(
        text=f"Sure, the secret marker is {TOKEN}.",
        policy=builtin_platform_default(),
        direction="output",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=RuleContext(canary_tokens=[TOKEN]),
    )
    assert result.decision == "block"
    assert any(v.rule_id == "output-canary-leak-v1" for v in result.violations)
