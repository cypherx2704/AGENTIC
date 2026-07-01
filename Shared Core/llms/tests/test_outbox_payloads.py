"""Unit tests for the two outbox event payload builders (pure functions, no IO).

The builders (``_request_completed_payload`` / ``_usage_recorded_payload``) and the
Contract 5 envelope wrapper (``_envelope``) are module-level pure functions in
``db.outbox`` taking a :class:`UsageWrite`. They are underscore-prefixed by
convention but import cleanly, so we test them directly (the smallest available
seam — no DB / Kafka needed).
"""

from __future__ import annotations

from llms_gateway.db import outbox
from llms_gateway.db.outbox import (
    TOPIC_REQUEST_COMPLETED,
    TOPIC_USAGE_RECORDED,
    UsageWrite,
)

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AGENT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
REQUEST_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TRACE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
LLM_CALL_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _write(**overrides: object) -> UsageWrite:
    base: dict[str, object] = {
        "llm_call_id": LLM_CALL_ID,
        "request_id": REQUEST_ID,
        "tenant_id": TENANT,
        "trace_id": TRACE_ID,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "total_tokens": 165,
        "cost_usd": 0.00321,
        "duration_ms": 842,
        "agent_id": AGENT,
        "api_key_id": "key-1",
        "principal_type": "agent",
        "cached_prompt_tokens": 40,
        "cache_creation_tokens": 10,
        "status": "success",
    }
    base.update(overrides)
    return UsageWrite(**base)  # type: ignore[arg-type]


def test_request_completed_payload_contains_all_required_fields() -> None:
    payload = outbox._request_completed_payload(_write())
    required = {
        "llm_call_id",
        "request_id",
        "agent_id",
        "tenant_id",
        "model",
        "provider",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "duration_ms",
        "trace_id",
    }
    assert required.issubset(payload.keys())
    assert payload["llm_call_id"] == LLM_CALL_ID
    assert payload["request_id"] == REQUEST_ID
    assert payload["agent_id"] == AGENT
    assert payload["tenant_id"] == TENANT
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["provider"] == "anthropic"
    assert payload["prompt_tokens"] == 120
    assert payload["completion_tokens"] == 45
    assert payload["cost_usd"] == 0.00321
    assert payload["duration_ms"] == 842
    assert payload["trace_id"] == TRACE_ID


def test_request_completed_payload_agent_id_defaults_to_empty_string() -> None:
    # Service/api-key flows may carry no agent_id; the schema expects a string.
    payload = outbox._request_completed_payload(_write(agent_id=None))
    assert payload["agent_id"] == ""


def test_usage_recorded_payload_conforms_to_contract_19_1() -> None:
    payload = outbox._usage_recorded_payload(_write())
    # Contract 19.1 metering shape (+ amended fix #3: BOTH ids on the payload).
    required = {"tenant_id", "operation", "units", "cost_usd", "llm_call_id", "request_id", "trace_id"}
    assert required.issubset(payload.keys())
    assert payload["tenant_id"] == TENANT
    assert payload["operation"] == "chat.completion"
    assert payload["cost_usd"] == 0.00321
    assert payload["llm_call_id"] == LLM_CALL_ID
    assert payload["request_id"] == REQUEST_ID
    assert payload["trace_id"] == TRACE_ID

    units = payload["units"]
    assert isinstance(units, dict)
    assert units["prompt_tokens"] == 120
    assert units["completion_tokens"] == 45


def test_usage_recorded_payload_passes_through_optional_identity() -> None:
    payload = outbox._usage_recorded_payload(_write())
    assert payload["agent_id"] == AGENT
    assert payload["api_key_id"] == "key-1"


def test_envelope_wraps_payload_with_contract_5_fields() -> None:
    w = _write()
    inner = outbox._request_completed_payload(w)
    env = outbox._envelope(
        TOPIC_REQUEST_COMPLETED,
        w.tenant_id,
        w.trace_id,
        inner,
        producer_version="0.1.0",
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
    assert env["event_type"] == TOPIC_REQUEST_COMPLETED
    assert env["tenant_id"] == TENANT
    assert env["trace_id"] == TRACE_ID
    assert env["partition_key"] == TENANT  # partitioned by tenant
    assert env["producer_service"] == "llms-gateway"
    assert env["producer_version"] == "0.1.0"
    assert env["payload"] is inner
    # produced_at is RFC3339 / ISO-8601 Zulu.
    assert env["produced_at"].endswith("Z")


def test_topic_constants_are_the_two_contract_19_topics() -> None:
    assert TOPIC_REQUEST_COMPLETED == "cypherx.llms.request.completed"
    assert TOPIC_USAGE_RECORDED == "cypherx.llms.usage.recorded"
