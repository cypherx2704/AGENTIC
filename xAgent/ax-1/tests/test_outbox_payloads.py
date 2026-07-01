"""Unit tests for the terminal Kafka event payload builders (``db/outbox.py``).

The payload builders + Contract 5 envelope wrapper are pure functions taking a
:class:`TaskEventWrite` — the smallest seam (no DB / Kafka needed). They cover:

  * the ``cypherx.agent.task.completed`` payload shape (task_id / agent_id / tenant_id /
    status / tokens_used / cost_usd / duration_ms / trace_id);
  * **FIX 1** — the ``cypherx.agent.task.failed`` payload uses ``error_message`` (NOT
    ``error_msg``; the DB column keeps the legacy name, but the Kafka payload field per
    agent.task.failed.schema.json is ``error_message``), and ``error_message`` is a
    REQUIRED field on the failed event;
  * the Contract 5 envelope wrapper (partition_key = tenant_id, producer_service, ...).
"""

from __future__ import annotations

from agent_runtime.db import outbox
from agent_runtime.db.outbox import (
    PRODUCER_SERVICE,
    TOPIC_TASK_COMPLETED,
    TOPIC_TASK_FAILED,
    TaskEventWrite,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AGENT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TRACE_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _completed_write(**overrides: object) -> TaskEventWrite:
    base: dict[str, object] = {
        "task_id": TASK_ID,
        "tenant_id": TENANT,
        "agent_id": AGENT,
        "trace_id": TRACE_ID,
        "status": "completed",
        "tokens_used": 128,
        "cost_usd": 0.0042,
        "duration_ms": 365,
        "output": {"message": "Hello!"},
    }
    base.update(overrides)
    return TaskEventWrite(**base)  # type: ignore[arg-type]


def _failed_write(**overrides: object) -> TaskEventWrite:
    base: dict[str, object] = {
        "task_id": TASK_ID,
        "tenant_id": TENANT,
        "agent_id": AGENT,
        "trace_id": TRACE_ID,
        "status": "failed",
        "error_code": "GUARDRAIL_VIOLATION",
        "error_message": "Prompt injection detected.",
    }
    base.update(overrides)
    return TaskEventWrite(**base)  # type: ignore[arg-type]


# ── completed payload ──────────────────────────────────────────────────────────────
def test_completed_payload_has_all_required_fields() -> None:
    payload = outbox._completed_payload(_completed_write())
    for key in (
        "task_id",
        "agent_id",
        "tenant_id",
        "status",
        "tokens_used",
        "cost_usd",
        "duration_ms",
        "trace_id",
    ):
        assert key in payload, f"missing required field {key}"
    assert payload["task_id"] == TASK_ID
    assert payload["agent_id"] == AGENT
    assert payload["tenant_id"] == TENANT
    assert payload["status"] == "completed"
    assert payload["tokens_used"] == 128
    assert payload["cost_usd"] == 0.0042
    assert payload["duration_ms"] == 365
    assert payload["trace_id"] == TRACE_ID


# ── failed payload (FIX 1) ───────────────────────────────────────────────────────────
def test_failed_payload_uses_error_message_field() -> None:
    payload = outbox._failed_payload(_failed_write())
    # FIX 1 — the field is error_message, NOT error_msg.
    assert "error_message" in payload
    assert "error_msg" not in payload
    assert payload["error_message"] == "Prompt injection detected."
    assert payload["error_code"] == "GUARDRAIL_VIOLATION"
    assert payload["task_id"] == TASK_ID
    assert payload["agent_id"] == AGENT
    assert payload["tenant_id"] == TENANT
    assert payload["trace_id"] == TRACE_ID


def test_failed_payload_error_message_is_required_with_fallback() -> None:
    # agent.task.failed.schema.json REQUIRES error_message: never None / absent, even
    # when the write carried neither code nor message.
    payload = outbox._failed_payload(_failed_write(error_code=None, error_message=None))
    assert payload["error_message"]  # non-empty fallback
    assert payload["error_code"]  # non-empty fallback


# ── Contract 5 envelope wrapper ──────────────────────────────────────────────────────
def test_completed_envelope_contract5_fields() -> None:
    w = _completed_write()
    inner = outbox._completed_payload(w)
    env = outbox._envelope(
        TOPIC_TASK_COMPLETED, w.tenant_id, w.trace_id, inner, producer_version="0.1.0"
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
    assert env["event_type"] == TOPIC_TASK_COMPLETED
    assert env["partition_key"] == TENANT  # partition by tenant
    assert env["tenant_id"] == TENANT
    assert env["producer_service"] == PRODUCER_SERVICE == "agent-runtime"
    assert env["producer_version"] == "0.1.0"
    assert env["schema_version"] == "1.0.0"
    assert env["produced_at"].endswith("Z")
    assert env["payload"] == inner


def test_failed_envelope_event_type() -> None:
    w = _failed_write()
    env = outbox._envelope(
        TOPIC_TASK_FAILED, w.tenant_id, w.trace_id, outbox._failed_payload(w), producer_version="0.1.0"
    )
    assert env["event_type"] == TOPIC_TASK_FAILED
    assert env["payload"]["error_message"] == "Prompt injection detected."


def test_topic_constants() -> None:
    assert TOPIC_TASK_COMPLETED == "cypherx.agent.task.completed"
    assert TOPIC_TASK_FAILED == "cypherx.agent.task.failed"
