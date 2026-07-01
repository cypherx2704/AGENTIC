"""Transactional outbox: violation row(s) + Kafka events in one tenant transaction.

Within a SINGLE tenant transaction we INSERT one ``guardrails.violations`` row per
fired rule AND ``guardrails.outbox`` rows for the two Kafka events below. A background
publisher task drains unpublished rows to Kafka via the Contract 5 envelope, marking
``published_at`` on success or incrementing ``attempts`` / ``last_error`` on failure.
Kafka connection failures never crash the request path (fail-soft) — the publisher logs
a WARN and retries; rows DLQ after 10 attempts.

Two events per check that fires (Component 4 + 5d):

* **FIX A** — ``cypherx.guardrails.violation.detected`` — payload's REQUIRED field is
  ``policy`` (the effective policy id/name), plus ``agent_id``, ``tenant_id``,
  ``direction``, ``decision``, ``trace_id`` and extras (``policy_id``, ``check_id``,
  ``request_id``, ``rule_ids``, ``severity``). Conforms to
  contracts/kafka/events/guardrails.violation.detected.schema.json.
* **FIX B** — ``cypherx.guardrails.usage.recorded`` — Contract 19.1 metering payload
  ``{tenant_id, api_key_id, agent_id, operation: 'check.input'|'check.output',
  units:{input_bytes, rules_evaluated}, cost_usd, duration_ms, request_id, trace_id}``.

Both wrapped in the Contract 5 envelope, ``partition_key = tenant_id``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .pool import in_tenant

logger = structlog.get_logger(__name__)

TOPIC_VIOLATION_DETECTED = "cypherx.guardrails.violation.detected"
TOPIC_USAGE_RECORDED = "cypherx.guardrails.usage.recorded"
TOPIC_POLICY_CHANGED = "cypherx.guardrails.policy.changed"
PRODUCER_SERVICE = "guardrails-service"
_DLQ_SUFFIX = ".dlq"
_MAX_ATTEMPTS = 10


@dataclass
class ViolationRow:
    """One fired rule, ready to persist + describe in the violation event."""

    rule_id: str
    rule_name: str
    severity: str
    category: str
    matched_text: str  # SAFE: redaction token / <=64-char truncation (FIX C)
    action: str


@dataclass
class CheckWrite:
    """Everything needed to persist a check's violations + emit the two outbox events."""

    check_id: str
    request_id: str
    tenant_id: str
    trace_id: str
    direction: str  # 'input' | 'output'
    decision: str  # 'allow' | 'warn' | 'redact' | 'block'
    policy_id: str
    policy_name: str
    violations: list[ViolationRow]
    agent_id: str | None = None
    api_key_id: str | None = None
    task_id: str | None = None
    input_bytes: int = 0
    output_bytes: int = 0
    rules_evaluated: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    tags: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _envelope(
    event_type: str,
    tenant_id: str,
    trace_id: str,
    payload: dict[str, Any],
    *,
    producer_version: str,
) -> dict[str, Any]:
    """Wrap a payload in the Contract 5 event envelope (partition_key = tenant_id)."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": "1.0.0",
        "produced_at": _now_iso(),
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "producer_service": PRODUCER_SERVICE,
        "producer_version": producer_version,
        "partition_key": tenant_id,
        "payload": payload,
    }


def _violation_payload(w: CheckWrite) -> dict[str, Any]:
    """FIX A — required field is ``policy`` (effective policy name); rich extras included."""
    return {
        # Contract-required fields (guardrails.violation.detected.schema.json):
        "agent_id": w.agent_id or "",
        "tenant_id": w.tenant_id,
        "policy": w.policy_name,
        "direction": w.direction,
        "decision": w.decision,
        "trace_id": w.trace_id,
        # Extras (forward-compatible; consumers tolerate unknown fields):
        "policy_id": w.policy_id,
        "check_id": w.check_id,
        "request_id": w.request_id,
        "task_id": w.task_id,
        "rule_ids": [v.rule_id for v in w.violations],
        "severity": _max_severity(w.violations),
    }


def _usage_payload(w: CheckWrite) -> dict[str, Any]:
    """FIX B — Contract 19.1 metering payload for the check."""
    operation = "check.input" if w.direction == "input" else "check.output"
    return {
        "tenant_id": w.tenant_id,
        "api_key_id": w.api_key_id,
        "agent_id": w.agent_id,
        "operation": operation,
        "units": {
            "input_bytes": w.input_bytes,
            "output_bytes": w.output_bytes,
            "rules_evaluated": w.rules_evaluated,
        },
        "cost_usd": w.cost_usd,
        "duration_ms": w.duration_ms,
        "request_id": w.request_id,
        "trace_id": w.trace_id,
    }


_SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _max_severity(violations: list[ViolationRow]) -> str:
    if not violations:
        return "info"
    return max(violations, key=lambda v: _SEVERITY_ORDER.get(v.severity, 0)).severity


async def record_check(
    pool: AsyncConnectionPool,
    w: CheckWrite,
    *,
    producer_version: str,
) -> None:
    """Persist violation rows + the two outbox events in one tenant transaction.

    The usage event is always emitted (metering is never sampled). The violation event
    + violation rows are written only when at least one rule fired.
    """

    async def _txn(conn: AsyncConnection) -> None:
        for v in w.violations:
            await conn.execute(
                """
                INSERT INTO guardrails.violations
                  (check_id, request_id, tenant_id, agent_id, task_id, trace_id, policy_id,
                   direction, decision, rule_id, rule_name, severity, category, matched_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    w.check_id, w.request_id, w.tenant_id, w.agent_id, w.task_id, w.trace_id,
                    w.policy_id, w.direction, w.decision, v.rule_id, v.rule_name, v.severity,
                    v.category, v.matched_text,
                ),
            )

        events: list[tuple[str, dict[str, Any]]] = [(TOPIC_USAGE_RECORDED, _usage_payload(w))]
        if w.violations:
            events.insert(0, (TOPIC_VIOLATION_DETECTED, _violation_payload(w)))

        for topic, payload in events:
            envelope = _envelope(topic, w.tenant_id, w.trace_id, payload, producer_version=producer_version)
            await conn.execute(
                "INSERT INTO guardrails.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
                (topic, w.tenant_id, Jsonb(envelope)),
            )

    await in_tenant(pool, w.tenant_id, _txn)


@dataclass
class UsageWrite:
    """A standalone metering event (no violations) — e.g. policy simulation (WP07).

    Mirrors the Contract 19.1 usage payload but for non-check operations. ``cost_usd`` is
    expected to be 0 for simulate (never billed); ``operation`` distinguishes it from a
    real ``check.input`` / ``check.output`` so consumers can exclude it from billing.
    """

    tenant_id: str
    operation: str            # e.g. 'simulate'
    request_id: str
    trace_id: str
    agent_id: str | None = None
    api_key_id: str | None = None
    input_bytes: int = 0
    rules_evaluated: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


async def record_usage(
    pool: AsyncConnectionPool,
    u: UsageWrite,
    *,
    producer_version: str,
) -> None:
    """Emit a single ``usage.recorded`` outbox event in one tenant transaction.

    Used by the simulate endpoint to meter a simulation as ``operation='simulate'`` with
    ``cost_usd=0`` WITHOUT writing any violation rows — a simulation never persists a real
    violation or bills usage. Wrapped in the Contract 5 envelope, ``partition_key=tenant_id``.
    """
    payload = {
        "tenant_id": u.tenant_id,
        "api_key_id": u.api_key_id,
        "agent_id": u.agent_id,
        "operation": u.operation,
        "units": {"input_bytes": u.input_bytes, "rules_evaluated": u.rules_evaluated},
        "cost_usd": u.cost_usd,
        "duration_ms": u.duration_ms,
        "request_id": u.request_id,
        "trace_id": u.trace_id,
    }

    async def _txn(conn: AsyncConnection) -> None:
        envelope = _envelope(
            TOPIC_USAGE_RECORDED, u.tenant_id, u.trace_id, payload,
            producer_version=producer_version,
        )
        await conn.execute(
            "INSERT INTO guardrails.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
            (TOPIC_USAGE_RECORDED, u.tenant_id, Jsonb(envelope)),
        )

    await in_tenant(pool, u.tenant_id, _txn)


async def record_policy_change(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    trace_id: str,
    action: str,
    root_policy_id: str,
    policy_id: str,
    details: dict[str, Any],
    producer_version: str,
) -> None:
    """Insert a ``policy.changed`` outbox event on the GIVEN connection (caller's txn).

    Takes a live connection (NOT a pool) so the event is written in the SAME tenant
    transaction as the policy state change — atomic with the create/edit/assign. Used to
    AUDIT fail_mode_override changes (and other policy state changes) onto the event bus.
    ``details`` carries redaction-safe metadata only (no rule content / no PII).
    """
    payload = {
        "tenant_id": tenant_id,
        "action": action,
        "policy_id": root_policy_id,
        "version_id": policy_id,
        "trace_id": trace_id,
        "details": details,
    }
    envelope = _envelope(
        TOPIC_POLICY_CHANGED, tenant_id, trace_id, payload, producer_version=producer_version
    )
    await conn.execute(
        "INSERT INTO guardrails.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
        (TOPIC_POLICY_CHANGED, tenant_id, Jsonb(envelope)),
    )


class OutboxPublisher:
    """Background task that drains ``guardrails.outbox`` to Kafka via aiokafka."""

    def __init__(
        self, pool: AsyncConnectionPool, kafka_brokers: str, *, poll_interval: float = 2.0
    ) -> None:
        self._pool = pool
        self._brokers = kafka_brokers
        self._poll_interval = poll_interval
        self._producer: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="outbox-publisher")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task
        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("kafka_producer_stop_failed", error=str(exc))

    async def _ensure_producer(self) -> Any | None:
        """Lazily create + start the aiokafka producer; return None on connect failure."""
        if self._producer is not None:
            return self._producer
        try:
            from aiokafka import AIOKafkaProducer

            producer = AIOKafkaProducer(
                bootstrap_servers=self._brokers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
            )
            await producer.start()
            self._producer = producer
            logger.info("kafka_producer_started", brokers=self._brokers)
            return producer
        except Exception as exc:  # noqa: BLE001 — never crash the request path
            logger.warning("kafka_producer_unavailable", error=str(exc))
            return None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._drain_once()
            except Exception as exc:  # noqa: BLE001 — publisher must keep running
                logger.warning("outbox_drain_error", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)

    async def _drain_once(self) -> None:
        producer = await self._ensure_producer()
        if producer is None:
            return  # Kafka down — retry next tick (events stay durable in the outbox).

        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT id, topic, partition_key, payload, attempts
                  FROM guardrails.outbox
                 WHERE published_at IS NULL
                 ORDER BY created_at
                 LIMIT 100
                """
            )
            rows = await cur.fetchall()

        for row_id, topic, partition_key, payload, attempts in rows:
            try:
                await producer.send_and_wait(topic, value=payload, key=partition_key)
            except Exception as exc:  # noqa: BLE001 — per-row failure handling
                await self._mark_failure(row_id, topic, partition_key, payload, attempts, str(exc))
                continue
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE guardrails.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
                )

    async def _mark_failure(
        self,
        row_id: str,
        topic: str,
        partition_key: str,
        payload: dict[str, Any],
        attempts: int,
        error: str,
    ) -> None:
        new_attempts = attempts + 1
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE guardrails.outbox SET attempts = %s, last_error = %s WHERE id = %s",
                (new_attempts, error[:2000], row_id),
            )
        if new_attempts >= _MAX_ATTEMPTS and self._producer is not None:
            try:
                await self._producer.send_and_wait(
                    topic + _DLQ_SUFFIX, value=payload, key=partition_key
                )
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE guardrails.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
                    )
                logger.warning("outbox_row_dlq", row_id=str(row_id), topic=topic)
            except Exception as exc:  # noqa: BLE001 — DLQ best-effort
                logger.warning("outbox_dlq_failed", row_id=str(row_id), error=str(exc))
