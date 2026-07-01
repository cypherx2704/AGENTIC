"""Unit tests for the two outbox event payload builders (pure functions, no IO).

Covers FIX A (``cypherx.guardrails.violation.detected`` REQUIRED field is ``policy``)
and FIX B (``cypherx.guardrails.usage.recorded`` conforms to Contract 19.1) plus the
Contract 5 envelope wrapper. The builders are module-level pure functions taking a
:class:`CheckWrite` — the smallest available seam (no DB / Kafka needed).
"""

from __future__ import annotations

from guardrails_service.db import outbox
from guardrails_service.db.outbox import (
    TOPIC_USAGE_RECORDED,
    TOPIC_VIOLATION_DETECTED,
    CheckWrite,
    ViolationRow,
)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AGENT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
REQUEST_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TRACE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
CHECK_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
POLICY_ID = "00000000-0000-0000-0000-0000000d0001"


def _write(**overrides: object) -> CheckWrite:
    base: dict[str, object] = {
        "check_id": CHECK_ID,
        "request_id": REQUEST_ID,
        "tenant_id": TENANT,
        "trace_id": TRACE_ID,
        "direction": "input",
        "decision": "block",
        "policy_id": POLICY_ID,
        "policy_name": "Platform Default Policy",
        "violations": [
            ViolationRow(
                rule_id="prompt-injection-v1",
                rule_name="Prompt Injection Detector",
                severity="critical",
                category="security",
                matched_text="ignore previous instruct",
                action="block",
            )
        ],
        "agent_id": AGENT,
        "api_key_id": "key-1",
        "task_id": None,
        "input_bytes": 42,
        "rules_evaluated": 6,
        "cost_usd": 0.00002,
        "duration_ms": 18,
    }
    base.update(overrides)
    return CheckWrite(**base)  # type: ignore[arg-type]


def test_violation_payload_required_policy_field() -> None:
    payload = outbox._violation_payload(_write())
    # FIX A — the schema's REQUIRED field is `policy` (the effective policy name).
    for key in ("agent_id", "tenant_id", "policy", "direction", "decision", "trace_id"):
        assert key in payload, f"missing required field {key}"
    assert payload["policy"] == "Platform Default Policy"
    assert payload["direction"] == "input"
    assert payload["decision"] == "block"
    assert payload["trace_id"] == TRACE_ID
    # Extras.
    assert payload["policy_id"] == POLICY_ID
    assert payload["check_id"] == CHECK_ID
    assert payload["request_id"] == REQUEST_ID
    assert payload["rule_ids"] == ["prompt-injection-v1"]
    assert payload["severity"] == "critical"


def test_usage_payload_contract_19_1() -> None:
    payload = outbox._usage_payload(_write())
    required = {"tenant_id", "operation", "units"}
    assert required.issubset(payload.keys())
    assert payload["tenant_id"] == TENANT
    assert payload["operation"] == "check.input"
    units = payload["units"]
    assert isinstance(units, dict)
    assert units["input_bytes"] == 42
    assert units["rules_evaluated"] == 6
    assert payload["cost_usd"] == 0.00002
    assert payload["duration_ms"] == 18
    assert payload["request_id"] == REQUEST_ID
    assert payload["trace_id"] == TRACE_ID


def test_usage_payload_output_operation() -> None:
    payload = outbox._usage_payload(_write(direction="output"))
    assert payload["operation"] == "check.output"


def test_envelope_contract_5_fields() -> None:
    w = _write()
    inner = outbox._violation_payload(w)
    env = outbox._envelope(
        TOPIC_VIOLATION_DETECTED, w.tenant_id, w.trace_id, inner, producer_version="0.1.0"
    )
    for key in (
        "event_id",
        "event_type",
        "schema_version",
        "produced_at",
        "trace_id",
        "tenant_id",
        "producer_service",
        "producer_version",
        "partition_key",
        "payload",
    ):
        assert key in env, f"missing envelope field {key}"
    assert env["event_type"] == TOPIC_VIOLATION_DETECTED
    assert env["partition_key"] == TENANT
    assert env["producer_service"] == "guardrails-service"
    assert env["produced_at"].endswith("Z")


def test_topic_constants() -> None:
    assert TOPIC_VIOLATION_DETECTED == "cypherx.guardrails.violation.detected"
    assert TOPIC_USAGE_RECORDED == "cypherx.guardrails.usage.recorded"
