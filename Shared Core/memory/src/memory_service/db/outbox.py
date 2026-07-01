"""Transactional outbox: domain row(s) + Kafka events in ONE tenant transaction.

The Memory service emits Contract-5-enveloped events on the same DB transaction that
mutates state, so the DB write and the Kafka event never diverge:

* ``cypherx.memory.stored``   — a memory was stored / bumped (Contract-19 metering joins
  on this for storage accounting).
* ``cypherx.memory.deleted``  — a memory (or a GDPR bulk wipe) deleted rows.
* ``cypherx.memory.gdpr.wiped`` — a principal's memories were bulk-wiped (the GDPR audit
  event; written in the SAME txn as the gdpr_wipe_log row + the DELETE).

A background publisher task drains unpublished rows to Kafka via aiokafka, marking
``published_at`` on success or incrementing ``attempts`` / ``last_error`` on failure.
Kafka connection failures never crash the request path — the publisher logs a WARN and
retries; rows DLQ after 10 attempts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger(__name__)

TOPIC_MEMORY_STORED = "cypherx.memory.stored"
TOPIC_MEMORY_DELETED = "cypherx.memory.deleted"
TOPIC_GDPR_WIPED = "cypherx.memory.gdpr.wiped"
# Contract-19.1 usage metering (fixes the previously-missing usage event). Payload shape
# is contracts/kafka/events/memory.usage.recorded.schema.json (Contract-5 envelope wraps
# it). Emitted via THIS outbox so metering is never sampled / lost.
TOPIC_MEMORY_USAGE_RECORDED = "cypherx.memory.usage.recorded"
PRODUCER_SERVICE = "memory-service"
_DLQ_SUFFIX = ".dlq"
_MAX_ATTEMPTS = 10


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def envelope(
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


async def emit(
    conn: AsyncConnection,
    *,
    topic: str,
    tenant_id: str,
    trace_id: str,
    payload: dict[str, Any],
    producer_version: str,
) -> None:
    """Insert one outbox event on the GIVEN connection (the caller's tenant txn).

    Takes a live connection (NOT a pool) so the event row is written in the SAME
    transaction as the state change — atomic with the store / delete / GDPR wipe.
    """
    env = envelope(topic, tenant_id, trace_id, payload, producer_version=producer_version)
    await conn.execute(
        "INSERT INTO memory.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
        (topic, tenant_id, Jsonb(env)),
    )


class OutboxPublisher:
    """Background task that drains ``memory.outbox`` to Kafka via aiokafka."""

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
                  FROM memory.outbox
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
                    "UPDATE memory.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
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
                "UPDATE memory.outbox SET attempts = %s, last_error = %s WHERE id = %s",
                (new_attempts, error[:2000], row_id),
            )
        if new_attempts >= _MAX_ATTEMPTS and self._producer is not None:
            try:
                await self._producer.send_and_wait(topic + _DLQ_SUFFIX, value=payload, key=partition_key)
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE memory.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
                    )
                logger.warning("outbox_row_dlq", row_id=str(row_id), topic=topic)
            except Exception as exc:  # noqa: BLE001 — DLQ best-effort
                logger.warning("outbox_dlq_failed", row_id=str(row_id), error=str(exc))
