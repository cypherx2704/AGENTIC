"""Transactional outbox: task finalize + Kafka event in ONE tenant transaction (Component 3b).

The EVENT stage finalises a task and emits exactly one Kafka event. To guarantee the
``xagent.tasks`` UPDATE and the ``cypherx.agent.task.completed`` /
``cypherx.agent.task.failed`` event can never diverge, ``record_task_event`` does
BOTH writes inside a single ``in_tenant`` transaction. A background ``OutboxPublisher``
drains unpublished ``xagent.outbox`` rows to Kafka via aiokafka using the Contract 5
envelope (``partition_key = tenant_id``), marking ``published_at`` on success and
DLQ-ing after 10 attempts.

``xagent.outbox`` has NO RLS (it is an internal cross-tenant publish queue drained by a
background task with no ``app.tenant_id`` set) — isolation lives in the payload, not the
row. The atomic write therefore inserts the outbox row inside the same tenant tx as the
task UPDATE, but the table itself is not RLS-protected.

BAKED-IN FIX 1: the ``cypherx.agent.task.failed`` payload field is ``error_message``
(NOT ``error_msg``) — the ``xagent.tasks.error_msg`` COLUMN keeps its name, but the
Kafka payload uses ``error_message`` per agent.task.failed.schema.json.
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

TOPIC_TASK_COMPLETED = "cypherx.agent.task.completed"
TOPIC_TASK_FAILED = "cypherx.agent.task.failed"
PRODUCER_SERVICE = "agent-runtime"
_DLQ_SUFFIX = ".dlq"
_MAX_ATTEMPTS = 10


@dataclass
class TaskEventWrite:
    """All fields needed to finalise a task row + emit its terminal Kafka event."""

    task_id: str
    tenant_id: str
    agent_id: str
    trace_id: str
    status: str  # completed | failed | cancelled | timeout
    tokens_used: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None  # FIX 1 — payload field name (col is error_msg)


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


def _completed_payload(w: TaskEventWrite) -> dict[str, Any]:
    return {
        "task_id": w.task_id,
        "agent_id": w.agent_id,
        "tenant_id": w.tenant_id,
        "status": w.status,
        "tokens_used": w.tokens_used,
        "cost_usd": w.cost_usd,
        "duration_ms": w.duration_ms,
        "trace_id": w.trace_id,
    }


def _failed_payload(w: TaskEventWrite) -> dict[str, Any]:
    # FIX 1 — failed payload uses error_message (agent.task.failed.schema.json).
    return {
        "task_id": w.task_id,
        "agent_id": w.agent_id,
        "tenant_id": w.tenant_id,
        "error_code": w.error_code or "INTERNAL_ERROR",
        "error_message": w.error_message or "Task failed.",
        "trace_id": w.trace_id,
    }


async def record_task_event(
    pool: AsyncConnectionPool,
    w: TaskEventWrite,
    *,
    producer_version: str,
) -> None:
    """Finalise the task row + insert the terminal outbox event in one tenant tx.

    The ``error_msg`` COLUMN is written from ``w.error_message`` (the column keeps its
    legacy name); the Kafka payload uses ``error_message`` (FIX 1).
    """
    is_completed = w.status == "completed"
    topic = TOPIC_TASK_COMPLETED if is_completed else TOPIC_TASK_FAILED
    payload = _completed_payload(w) if is_completed else _failed_payload(w)
    envelope = _envelope(topic, w.tenant_id, w.trace_id, payload, producer_version=producer_version)

    async def _txn(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            UPDATE xagent.tasks
               SET status = %s, output = %s, tokens_used = %s, cost_usd = %s,
                   error_code = %s, error_msg = %s, completed_at = NOW()
             WHERE task_id = %s
            """,
            (
                w.status,
                Jsonb(w.output) if w.output is not None else None,
                w.tokens_used,
                w.cost_usd,
                w.error_code,
                w.error_message,
                w.task_id,
            ),
        )
        await conn.execute(
            "INSERT INTO xagent.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
            (topic, w.tenant_id, Jsonb(envelope)),
        )

    await in_tenant(pool, w.tenant_id, _txn)


async def record_metered_event(
    pool: AsyncConnectionPool,
    *,
    topic: str,
    tenant_id: str,
    trace_id: str,
    payload: dict[str, Any],
    producer_version: str,
) -> None:
    """Insert ONE arbitrary outbox event (Contract 5 envelope, partition_key = tenant_id).

    The WP12 metering hook: the tool-loop stage emits ``...tools.invocation.metered`` once
    per tool invocation so billing/usage can meter tool spend independently of the terminal
    task event. Standalone INSERT (NOT atomic with any task-row UPDATE — a metered event is
    an additive usage signal, not a state transition). The same background ``OutboxPublisher``
    drains it to Kafka. RLS does not apply to ``outbox`` (cross-tenant publish queue); the
    write runs inside an ``in_tenant`` tx only to share the pooled connection cleanly.
    """
    envelope = _envelope(topic, tenant_id, trace_id, payload, producer_version=producer_version)

    async def _txn(conn: AsyncConnection) -> None:
        await conn.execute(
            "INSERT INTO xagent.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
            (topic, tenant_id, Jsonb(envelope)),
        )

    await in_tenant(pool, tenant_id, _txn)


async def sweep_task_failed(
    pool: AsyncConnectionPool,
    *,
    task_id: str,
    tenant_id: str,
    agent_id: str,
    trace_id: str,
    error_code: str,
    error_message: str,
    producer_version: str,
) -> bool:
    """Backup-sweeper finalize: mark a STUCK task failed + insert its outbox row atomically.

    Runs in ONE tenant transaction (``in_tenant`` sets ``app.tenant_id`` for RLS), so the
    ``xagent.tasks`` UPDATE and the ``cypherx.agent.task.failed`` outbox INSERT can never
    diverge — exactly like ``record_task_event``, but with two sweeper-specific guards:

      * the UPDATE is GUARDED to ``status IN ('pending','running')`` so a task that the
        in-process pipeline finalised first is NOT clobbered (the sweeper is a backstop,
        not an authority) — if 0 rows update, we SKIP the outbox insert and return False
        (no duplicate terminal event);
      * status is always ``failed`` with a ``timeout``/stuck error code.

    Returns True iff this sweeper actually finalised the row (and emitted the event).
    """
    write = TaskEventWrite(
        task_id=task_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        trace_id=trace_id,
        status="failed",
        error_code=error_code,
        error_message=error_message,
    )
    envelope = _envelope(
        TOPIC_TASK_FAILED, tenant_id, trace_id, _failed_payload(write), producer_version=producer_version
    )

    async def _txn(conn: AsyncConnection) -> bool:
        cur = await conn.execute(
            """
            UPDATE xagent.tasks
               SET status = 'failed', error_code = %s, error_msg = %s, completed_at = NOW()
             WHERE task_id = %s
               AND status IN ('pending', 'running')
            """,
            (error_code, error_message, task_id),
        )
        if cur.rowcount == 0:
            return False  # already terminal (raced with the in-process finalize) — do nothing
        await conn.execute(
            "INSERT INTO xagent.outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
            (TOPIC_TASK_FAILED, tenant_id, Jsonb(envelope)),
        )
        return True

    return await in_tenant(pool, tenant_id, _txn)


class OutboxPublisher:
    """Background task that drains ``xagent.outbox`` to Kafka via aiokafka."""

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
            return  # Kafka down — retry next tick (events stay durable in the outbox).

        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=tuple_row).execute(
                """
                SELECT id, topic, partition_key, payload, attempts
                  FROM xagent.outbox
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
                await conn.execute("UPDATE xagent.outbox SET published_at = NOW() WHERE id = %s", (row_id,))

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
                "UPDATE xagent.outbox SET attempts = %s, last_error = %s WHERE id = %s",
                (new_attempts, error[:2000], row_id),
            )
        if new_attempts >= _MAX_ATTEMPTS and self._producer is not None:
            try:
                await self._producer.send_and_wait(topic + _DLQ_SUFFIX, value=payload, key=partition_key)
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE xagent.outbox SET published_at = NOW() WHERE id = %s", (row_id,)
                    )
                logger.warning("outbox_row_dlq", row_id=str(row_id), topic=topic)
            except Exception as exc:  # noqa: BLE001 — DLQ best-effort
                logger.warning("outbox_dlq_failed", row_id=str(row_id), error=str(exc))
