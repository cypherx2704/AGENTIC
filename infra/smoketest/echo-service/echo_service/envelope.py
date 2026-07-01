"""Contract 5 — Kafka event envelope builder.

Produces an object that validates against
contracts/kafka/event-envelope.schema.json. Required keys: event_id,
event_type, schema_version, produced_at, tenant_id, producer_service,
partition_key, payload. trace_id and producer_version are optional-but-included.

partition_key defaults to tenant_id (Contract 5 / topics.md §4). The
cypherx.smoketest.event topic is a normal (non-compact) delete topic, so the
tenant_id default is correct — the agent_id override is ONLY for compact
auth.agent.* topics (Component 17), which this is not.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any


def _now_rfc3339_ms() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def build_envelope(
    *,
    event_type: str,
    tenant_id: str,
    producer_service: str,
    payload: dict[str, Any],
    trace_id: str | None = None,
    producer_version: str = "0.1.0",
    schema_version: str = "1.0.0",
    partition_key: str | None = None,
) -> dict[str, Any]:
    """Build a Contract 5 envelope. `partition_key` defaults to `tenant_id`."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": schema_version,
        "produced_at": _now_rfc3339_ms(),
        "trace_id": trace_id or str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "producer_service": producer_service,
        "producer_version": producer_version,
        # Contract 5: defaults to tenant_id for tenant-scoped events.
        "partition_key": partition_key or tenant_id,
        "payload": payload,
    }
