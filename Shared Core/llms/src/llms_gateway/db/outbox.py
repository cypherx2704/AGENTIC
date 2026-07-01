"""Transactional outbox: usage record + Kafka events in one tenant transaction.

Within a SINGLE tenant transaction we INSERT into ``llms.usage_records`` AND two
``llms.outbox`` rows (Contract 19 reconciliation — see module docstring below).
A background publisher task polls unpublished rows and produces them to Kafka via
the Contract 5 envelope, marking ``published_at`` on success or incrementing
``attempts`` / ``last_error`` on failure. Kafka connection failures never crash the
request path — the publisher logs a WARN and retries.

**Contract 19 reconciliation — TWO events per completion:**

* ``cypherx.llms.request.completed`` — payload per
  kafka/events/llms.request.completed.schema.json.
* ``cypherx.llms.usage.recorded``   — payload per usage/usage-event.schema.json
  (the canonical metering topic downstream joiners subscribe to).

Both are written as outbox rows inside the same transaction as the usage record, so
the DB write and the Kafka events never diverge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .pool import in_tenant

logger = structlog.get_logger(__name__)

TOPIC_REQUEST_COMPLETED = "cypherx.llms.request.completed"
TOPIC_USAGE_RECORDED = "cypherx.llms.usage.recorded"
_DLQ_SUFFIX = ".dlq"
_MAX_ATTEMPTS = 10

# Maps the UsageWrite.operation (a usage_records column value) to the Contract-19
# metering `operation` key on the cypherx.llms.usage.recorded payload.
_METERING_OPERATION: dict[str, str] = {
    "chat": "chat.completion",
    "embedding": "embedding",
    "rerank": "rerank",
    "classify": "classify",
}


@dataclass
class UsageWrite:
    """All fields needed to persist a usage record + emit the two outbox events."""

    # Gateway-minted, one fresh UUIDv4 per provider call — THE billing uniqueness
    # key (amended fix #3: UNIQUE (tenant_id, llm_call_id)). request_id is
    # correlation-only and legitimately repeats across calls (Contract 8).
    llm_call_id: str
    request_id: str
    tenant_id: str
    trace_id: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    duration_ms: int
    agent_id: str | None = None
    api_key_id: str | None = None
    principal_type: str = "agent"
    task_id: str | None = None
    cached_prompt_tokens: int = 0
    cache_creation_tokens: int = 0
    status: str = "success"
    # The kind of call this row bills: "chat" (default) or "embedding" (WP06). Persisted
    # to usage_records.operation and mapped to the Contract-19 metering `operation` key.
    operation: str = "chat"


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
    """Wrap a payload in the Contract 5 event envelope."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": "1.0.0",
        "produced_at": _now_iso(),
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "producer_service": "llms-gateway",
        "producer_version": producer_version,
        "partition_key": tenant_id,
        "payload": payload,
    }


def _request_completed_payload(w: UsageWrite) -> dict[str, Any]:
    return {
        "llm_call_id": w.llm_call_id,
        "request_id": w.request_id,
        "agent_id": w.agent_id or "",
        "tenant_id": w.tenant_id,
        "model": w.model,
        "provider": w.provider,
        "prompt_tokens": w.prompt_tokens,
        "completion_tokens": w.completion_tokens,
        "cached_prompt_tokens": w.cached_prompt_tokens,
        "cache_creation_tokens": w.cache_creation_tokens,
        "cost_usd": w.cost_usd,
        "duration_ms": w.duration_ms,
        "status": w.status,
        "trace_id": w.trace_id,
    }


def _usage_recorded_payload(w: UsageWrite) -> dict[str, Any]:
    return {
        "tenant_id": w.tenant_id,
        "api_key_id": w.api_key_id,
        "agent_id": w.agent_id,
        "operation": _METERING_OPERATION.get(w.operation, w.operation),
        "units": {
            "prompt_tokens": w.prompt_tokens,
            "completion_tokens": w.completion_tokens,
        },
        "cost_usd": w.cost_usd,
        "duration_ms": w.duration_ms,
        "llm_call_id": w.llm_call_id,
        "request_id": w.request_id,
        "trace_id": w.trace_id,
    }


async def record_usage(
    pool: AsyncConnectionPool,
    w: UsageWrite,
    *,
    producer_version: str,
) -> None:
    """Persist a usage record + two outbox events in one tenant transaction."""

    async def _txn(conn: AsyncConnection) -> None:
        # NO ON CONFLICT on this hot path (amended): a duplicate llm_call_id here is
        # a BUG — fail loudly (unique violation) rather than silently drop a billing
        # row. Only the billing-replay worker (WP05) may use ON CONFLICT DO NOTHING.
        await conn.execute(
            """
            INSERT INTO llms.usage_records
              (llm_call_id, request_id, tenant_id, agent_id, api_key_id, principal_type, task_id,
               trace_id, provider, model, prompt_tokens, completion_tokens, total_tokens,
               cached_prompt_tokens, cache_creation_tokens, cost_usd, duration_ms, status, operation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                w.llm_call_id, w.request_id, w.tenant_id, w.agent_id, w.api_key_id,
                w.principal_type, w.task_id,
                w.trace_id, w.provider, w.model, w.prompt_tokens, w.completion_tokens, w.total_tokens,
                w.cached_prompt_tokens, w.cache_creation_tokens, w.cost_usd, w.duration_ms, w.status,
                w.operation,
            ),
        )
        for topic, payload in (
            (TOPIC_REQUEST_COMPLETED, _request_completed_payload(w)),
            (TOPIC_USAGE_RECORDED, _usage_recorded_payload(w)),
        ):
            envelope = _envelope(topic, w.tenant_id, w.trace_id, payload, producer_version=producer_version)
            await conn.execute(
                "INSERT INTO llms.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
                (topic, w.tenant_id, Jsonb(envelope)),
            )

    await in_tenant(pool, w.tenant_id, _txn)


class OutboxPublisher:
    """Background task that drains ``llms.outbox`` to Kafka via aiokafka."""

    def __init__(self, pool: AsyncConnectionPool, kafka_brokers: str, *, poll_interval: float = 2.0) -> None:
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
            return  # Kafka down — retry next tick.

        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT id, topic, partition_key, payload, attempts
                  FROM llms.outbox
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
                    "UPDATE llms.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
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
                "UPDATE llms.outbox SET attempts = %s, last_error = %s WHERE id = %s",
                (new_attempts, error[:2000], row_id),
            )
        if new_attempts >= _MAX_ATTEMPTS and self._producer is not None:
            try:
                await self._producer.send_and_wait(
                    topic + _DLQ_SUFFIX, value=payload, key=partition_key
                )
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE llms.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
                    )
                logger.warning("outbox_row_dlq", row_id=str(row_id), topic=topic)
            except Exception as exc:  # noqa: BLE001 — DLQ best-effort
                logger.warning("outbox_dlq_failed", row_id=str(row_id), error=str(exc))
