"""Unit tests for the B3 ICAO 9303 MRZ passport detector (regex + check digits)."""

from __future__ import annotations

from guardrails_service.services.pipeline import evaluate
from guardrails_service.services.policy_engine import builtin_platform_default
from guardrails_service.services.rules.definitions import (
    RuleContext,
    _mrz_check_digit,
    _mrz_td1_valid,
    _mrz_td2_valid,
    _mrz_td3_valid,
    detect_pii_passport_mrz,
)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KEY = "test-key"

# ICAO Doc 9303 TD3 (passport) specimen — all four check digits pass.
TD3_L1 = "P<UTOERIKSSON<<ANNA<MARIA".ljust(44, "<")
TD3_L2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<10"
# TD2 + TD1 specimens.
TD2_L1 = "I<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<"
TD2_L2 = "D231458907UTO7408122F1204159<<<<<<<6"
TD1_L1 = "I<UTOD231458907<<<<<<<<<<<<<<<"
TD1_L2 = "7408122F1204159UTO<<<<<<<<<<<6"
TD1_L3 = "ERIKSSON<<ANNA<MARIA<<<<<<<<<<"


def test_mrz_check_digit_known_values() -> None:
    assert _mrz_check_digit("L898902C3") == 6       # document number
    assert _mrz_check_digit("740812") == 2          # date of birth
    assert _mrz_check_digit("120415") == 9          # expiry
    # An out-of-alphabet character invalidates the field.
    assert _mrz_check_digit("ABC!DEF") == -1


def test_td3_td2_td1_validation() -> None:
    assert _mrz_td3_valid(TD3_L2) is True
    assert _mrz_td2_valid(TD2_L2) is True
    assert _mrz_td1_valid(TD1_L1, TD1_L2) is True


def test_valid_mrz_is_detected() -> None:
    hits = detect_pii_passport_mrz(f"Passport:\n{TD3_L1}\n{TD3_L2}", RuleContext())
    assert len(hits) == 1
    assert hits[0].category == "passport"


def test_checksum_failing_near_miss_is_ignored() -> None:
    # Corrupt the composite check digit -> not a valid MRZ -> no hit (near-miss => allow).
    bad = TD3_L2[:-1] + ("1" if TD3_L2[-1] != "1" else "2")
    hits = detect_pii_passport_mrz(f"Block:\n{TD3_L1}\n{bad}", RuleContext())
    assert hits == []


def test_mrz_redacts_raw_document_number() -> None:
    result = evaluate(
        text=f"On file:\n{TD3_L1}\n{TD3_L2}",
        policy=builtin_platform_default(),
        direction="input",
        tenant_id=TENANT,
        redaction_key=KEY,
        ctx=RuleContext(),
    )
    assert result.decision == "redact"
    pt = result.processed_text or ""
    assert "L898902C3" not in pt           # raw document number never leaves
    assert "[REDACTED:passport:" in pt     # deterministic HMAC token substituted


def test_no_mrz_in_plain_text_is_noop() -> None:
    assert detect_pii_passport_mrz("Just a normal sentence with no MRZ block.", RuleContext()) == []
