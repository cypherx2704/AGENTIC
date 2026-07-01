"""Kafka ingestion worker (Component 2) — consumes cypherx.rag.ingestion.requested.

The message is the authoritative work-order: the worker NEVER re-reads embedding/chunking
config from rag.knowledge_bases (it is snapshotted into the payload at finalize time). The
only DB reads/writes on the hot path are the document status transitions + chunk INSERTs.

Poison-pill / DLQ flow (a corrupt doc must not block the consumer group forever):
  * On failure: increment ``rag.documents.attempts`` + capture ``error_msg``.
  * attempts < max  -> RETRY (raise so the offset is NOT committed; redelivered w/ backoff).
  * attempts >= max -> DLQ: publish to ``<topic>.dlq``, set status='failed', emit
    ``cypherx.rag.ingestion.failed`` via outbox, COMMIT the offset (move on).

``process_message`` is the unit under test — driven with a fake consumer/producer + a
fake/real object store + the mock embedder. ``run_worker`` wires aiokafka around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..core import metrics, trace
from ..core.config import Settings
from ..db import outbox
from ..db.pool import in_tenant
from ..services.contextual import Contextualizer
from ..services.embeddings import EmbeddingClient
from ..services.ingest import ingest_text
from ..services.object_store import ObjectStore
from ..services.store.pgvector import PgVectorAdapter

logger = structlog.get_logger(__name__)


class PoisonPillError(Exception):
    """Raised after the worker DLQ's a message — the offset SHOULD be committed."""


class RetryableError(Exception):
    """Raised when processing failed but attempts remain — do NOT commit the offset."""


@dataclass
class WorkerDeps:
    pool: AsyncConnectionPool
    embedder: EmbeddingClient
    object_store: ObjectStore
    settings: Settings
    dlq_producer: Any | None = None  # anything with async send_and_wait(topic, value, key)
    contextualizer: Contextualizer | None = None  # optional (RAG_CONTEXTUAL_INGEST)


async def _bump_attempts(pool: AsyncConnectionPool, tenant_id: str, doc_id: str, error: str) -> int:
    async def _txn(conn: AsyncConnection) -> int:
        cur = await conn.execute(
            "UPDATE rag.documents SET attempts = attempts + 1, error_msg = %s "
            "WHERE doc_id = %s RETURNING attempts",
            (error[:2000], doc_id),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    return await in_tenant(pool, tenant_id, _txn)


async def _mark_processing(pool: AsyncConnectionPool, tenant_id: str, doc_id: str) -> None:
    async def _txn(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE rag.documents SET status = 'processing' WHERE doc_id = %s", (doc_id,)
        )

    await in_tenant(pool, tenant_id, _txn)


async def _complete(
    pool: AsyncConnectionPool,
    tenant_id: str,
    payload: dict[str, Any],
    chunks_indexed: int,
    embedding_tokens: int,
    *,
    producer_version: str,
) -> None:
    trace_id = payload.get("trace_id", "")
    request_id = payload.get("request_id", "")
    completed_payload = {
        "doc_id": payload["doc_id"],
        "kb_id": payload["kb_id"],
        "tenant_id": tenant_id,
        "chunk_count": chunks_indexed,
        "request_id": request_id,
        "trace_id": trace_id,
    }
    usage_payload = {
        "tenant_id": tenant_id,
        "agent_id": payload.get("agent_id"),
        "operation": "rag.ingest",
        "units": {"chunks_indexed": chunks_indexed, "embedding_tokens_used": embedding_tokens},
        "request_id": request_id,
        "trace_id": trace_id,
    }

    async def _txn(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE rag.documents SET status = 'completed', completed_at = NOW() WHERE doc_id = %s",
            (payload["doc_id"],),
        )
        await outbox.enqueue_outbox(
            conn, outbox.TOPIC_INGESTION_COMPLETED, tenant_id, trace_id, completed_payload,
            producer_version=producer_version,
        )
        await outbox.enqueue_outbox(
            conn, outbox.TOPIC_USAGE_RECORDED, tenant_id, trace_id, usage_payload,
            producer_version=producer_version,
        )

    await in_tenant(pool, tenant_id, _txn)


async def _fail_terminal(
    pool: AsyncConnectionPool,
    tenant_id: str,
    payload: dict[str, Any],
    error: str,
    attempts: int,
    *,
    producer_version: str,
) -> None:
    trace_id = payload.get("trace_id", "")
    failed_payload = {
        "doc_id": payload["doc_id"],
        "kb_id": payload["kb_id"],
        "tenant_id": tenant_id,
        "error_code": "INGESTION_FAILED",
        "error_msg": error[:2000],
        "attempts": attempts,
        "request_id": payload.get("request_id", ""),
        "trace_id": trace_id,
    }

    async def _txn(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE rag.documents SET status = 'failed', error_msg = %s WHERE doc_id = %s",
            (error[:2000], payload["doc_id"]),
        )
        await outbox.enqueue_outbox(
            conn, outbox.TOPIC_INGESTION_FAILED, tenant_id, trace_id, failed_payload,
            producer_version=producer_version,
        )

    await in_tenant(pool, tenant_id, _txn)


async def _load_text(deps: WorkerDeps, payload: dict[str, Any]) -> str:
    """Fetch + extract raw text for the document from object storage.

    First-cycle source types: markdown / text are decoded directly; pdf would parse via
    a text extractor in the cloud form. For the compose/test path the object store returns
    bytes (or the worker is handed text directly via the payload's ``inline_text``).
    """
    if "inline_text" in payload:
        return str(payload["inline_text"])
    key = payload["source_uri"].split(f"{deps.settings.s3_bucket}/", 1)[-1]
    body = await deps.object_store.get_object(key)
    return body.decode("utf-8", errors="replace")


async def process_message(deps: WorkerDeps, message: dict[str, Any]) -> str:
    """Process one ingestion work-order. The message is a Contract-5 envelope OR a bare
    payload dict (the worker accepts both for test ergonomics).

    Returns the outcome ('completed' | 'dlq'). Raises RetryableError when attempts remain
    (the caller must NOT commit the offset). Raises nothing on terminal success/DLQ.
    """
    payload = message.get("payload", message)
    tenant_id = payload["tenant_id"]
    doc_id = payload["doc_id"]
    # Bind correlation so the worker's logs + downstream embedding calls join the request.
    trace.request_id_var.set(payload.get("request_id", ""))
    trace.trace_id_var.set(payload.get("trace_id", ""))

    try:
        await _mark_processing(deps.pool, tenant_id, doc_id)
        text = await _load_text(deps, payload)
        store = PgVectorAdapter(deps.pool, deps.settings)
        result = await ingest_text(
            text=text,
            doc_id=doc_id,
            kb_id=payload["kb_id"],
            tenant_id=tenant_id,
            embedding_model=payload["embedding_model_resolved"],
            embedding_dim=payload["embedding_dim"],
            chunking_strategy=payload["chunking_strategy"],
            chunk_size=payload["chunk_size"],
            chunk_overlap=payload["chunk_overlap"],
            doc_name=payload.get("doc_name", doc_id),
            source_uri=payload.get("source_uri"),
            embedder=deps.embedder,
            store=store,
            settings=deps.settings,
            on_behalf_of=payload.get("agent_id"),
            contextualizer=deps.contextualizer,
        )
    except Exception as exc:  # noqa: BLE001 — failure path: poison-pill / retry
        attempts = await _bump_attempts(deps.pool, tenant_id, doc_id, str(exc))
        if attempts >= deps.settings.worker_max_attempts:
            await _dlq(deps, payload, str(exc), attempts)
            metrics.worker_processed_total.labels("dlq").inc()
            logger.warning("ingestion_dlq", doc_id=doc_id, attempts=attempts, error=str(exc))
            return "dlq"
        metrics.worker_processed_total.labels("retried").inc()
        logger.info("ingestion_retry", doc_id=doc_id, attempts=attempts, error=str(exc))
        raise RetryableError(str(exc)) from exc

    await _complete(
        deps.pool, tenant_id, payload, result.chunks_indexed, result.embedding_tokens_used,
        producer_version=deps.settings.service_version,
    )
    metrics.worker_processed_total.labels("completed").inc()
    metrics.chunks_indexed_total.inc(result.chunks_indexed)
    return "completed"


async def _dlq(deps: WorkerDeps, payload: dict[str, Any], error: str, attempts: int) -> None:
    """Publish to the DLQ topic + mark the document failed + emit the failed event."""
    if deps.dlq_producer is not None:
        dlq_topic = deps.settings.ingestion_topic + ".dlq"
        dlq_payload = {
            "doc_id": payload["doc_id"],
            "kb_id": payload["kb_id"],
            "tenant_id": payload["tenant_id"],
            "error_msg": error[:2000],
            "attempts": attempts,
        }
        with contextlib.suppress(Exception):
            await deps.dlq_producer.send_and_wait(
                dlq_topic, value=dlq_payload, key=payload["tenant_id"]
            )
    await _fail_terminal(
        deps.pool, payload["tenant_id"], payload, error, attempts,
        producer_version=deps.settings.service_version,
    )


async def run_worker(deps: WorkerDeps) -> None:  # pragma: no cover — live aiokafka loop
    """Wire aiokafka around ``process_message`` (manual commit; backoff on retry)."""
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    consumer = AIOKafkaConsumer(
        deps.settings.ingestion_topic,
        bootstrap_servers=deps.settings.kafka_brokers,
        group_id=deps.settings.ingestion_consumer_group,
        enable_auto_commit=False,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=deps.settings.kafka_brokers,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
    )
    deps.dlq_producer = producer
    await consumer.start()
    await producer.start()
    logger.info("ingestion_worker_started", topic=deps.settings.ingestion_topic)
    try:
        async for msg in consumer:
            try:
                await process_message(deps, msg.value)
                await consumer.commit()
            except RetryableError:
                # Do NOT commit — redelivered with backoff (pause/resume in the cloud form).
                await asyncio.sleep(min(30, 2 ** 4))
            except Exception as exc:  # noqa: BLE001 — never let one message kill the loop
                logger.warning("ingestion_worker_error", error=str(exc))
                await consumer.commit()
    finally:
        await consumer.stop()
        await producer.stop()
