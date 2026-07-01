"""Kafka ingestion worker: happy path, retry, DLQ/poison-pill, dedup."""

from __future__ import annotations

import pytest

from rag_service.core.config import Settings
from rag_service.db import outbox
from rag_service.services.embeddings import EmbeddingClient
from rag_service.worker.ingestion_worker import (
    RetryableError,
    WorkerDeps,
    process_message,
)

from .conftest import TEST_TENANT
from .fakes import FakeDb, FakePool

TENANT = TEST_TENANT


class _FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_and_wait(self, topic: str, value: dict, key: str | None = None) -> None:
        self.sent.append((topic, value))


def _settings() -> Settings:
    return Settings(mock_embeddings=True, worker_max_attempts=3)


def _seed_doc(db: FakeDb, doc_id: str, kb_id: str) -> None:
    db.knowledge_bases.append({
        "kb_id": kb_id, "tenant_id": TENANT, "name": "kb",
        "embedding_model_resolved": "text-embedding-3-small", "embedding_dim": 1536,
        "chunking_strategy": "sentence", "chunk_size": 512, "chunk_overlap": 50,
    })
    db.documents.append({
        "doc_id": doc_id, "kb_id": kb_id, "tenant_id": TENANT, "name": "d.md",
        "source_type": "markdown", "source_uri": "s3://b/x", "status": "pending",
        "attempts": 0, "error_msg": None, "metadata": {}, "created_at": None, "completed_at": None,
    })


def _msg(doc_id: str, kb_id: str, *, inline_text: str | None = "alpha beta gamma. delta.") -> dict:
    payload = {
        "doc_id": doc_id, "kb_id": kb_id, "tenant_id": TENANT,
        "source_uri": "s3://b/x", "source_type": "markdown",
        "embedding_model_resolved": "text-embedding-3-small", "embedding_dim": 1536,
        "chunking_strategy": "sentence", "chunk_size": 512, "chunk_overlap": 50,
        "request_id": "req-1", "trace_id": "trace-1", "agent_id": None,
    }
    if inline_text is not None:
        payload["inline_text"] = inline_text
    return {"payload": payload}


@pytest.mark.asyncio
async def test_worker_happy_path_completes() -> None:
    db = FakeDb()
    pool = FakePool(db)
    settings = _settings()
    _seed_doc(db, "doc-1", "kb-1")
    deps = WorkerDeps(
        pool=pool, embedder=EmbeddingClient(settings), object_store=None, settings=settings,
        dlq_producer=_FakeProducer(),
    )
    outcome = await process_message(deps, _msg("doc-1", "kb-1"))
    assert outcome == "completed"
    doc = next(d for d in db.documents if d["doc_id"] == "doc-1")
    assert doc["status"] == "completed"
    assert len(db.chunks) >= 1
    assert outbox.TOPIC_INGESTION_COMPLETED in db.outbox_topics()
    assert outbox.TOPIC_USAGE_RECORDED in db.outbox_topics()


@pytest.mark.asyncio
async def test_worker_retries_before_dlq() -> None:
    db = FakeDb()
    pool = FakePool(db)
    settings = _settings()
    _seed_doc(db, "doc-2", "kb-1")

    # An embedder that always raises -> the worker bumps attempts and asks for a retry.
    class _BoomEmbedder:
        async def embed(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("provider exploded")

    deps = WorkerDeps(
        pool=pool, embedder=_BoomEmbedder(), object_store=None, settings=settings,
        dlq_producer=_FakeProducer(),
    )
    # attempts 1 and 2 -> RetryableError (offset NOT committed).
    with pytest.raises(RetryableError):
        await process_message(deps, _msg("doc-2", "kb-1"))
    with pytest.raises(RetryableError):
        await process_message(deps, _msg("doc-2", "kb-1"))
    doc = next(d for d in db.documents if d["doc_id"] == "doc-2")
    assert doc["attempts"] == 2
    assert doc["status"] == "processing"


@pytest.mark.asyncio
async def test_worker_dlq_on_poison_pill() -> None:
    db = FakeDb()
    pool = FakePool(db)
    settings = _settings()
    _seed_doc(db, "doc-3", "kb-1")
    producer = _FakeProducer()

    class _BoomEmbedder:
        async def embed(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("corrupt doc")

    deps = WorkerDeps(
        pool=pool, embedder=_BoomEmbedder(), object_store=None, settings=settings,
        dlq_producer=producer,
    )
    # Three attempts: first two retry, the third (attempts == max) DLQs.
    with pytest.raises(RetryableError):
        await process_message(deps, _msg("doc-3", "kb-1"))
    with pytest.raises(RetryableError):
        await process_message(deps, _msg("doc-3", "kb-1"))
    outcome = await process_message(deps, _msg("doc-3", "kb-1"))
    assert outcome == "dlq"

    doc = next(d for d in db.documents if d["doc_id"] == "doc-3")
    assert doc["status"] == "failed"
    assert doc["attempts"] == 3
    # Published to the DLQ topic + emitted ingestion.failed.
    assert any(t.endswith(".dlq") for t, _ in producer.sent)
    assert outbox.TOPIC_INGESTION_FAILED in db.outbox_topics()


@pytest.mark.asyncio
async def test_worker_dedup_on_redelivery() -> None:
    db = FakeDb()
    pool = FakePool(db)
    settings = _settings()
    _seed_doc(db, "doc-4", "kb-1")
    deps = WorkerDeps(
        pool=pool, embedder=EmbeddingClient(settings), object_store=None, settings=settings,
        dlq_producer=_FakeProducer(),
    )
    await process_message(deps, _msg("doc-4", "kb-1"))
    chunks_after_first = len([c for c in db.chunks if c["doc_id"] == "doc-4"])
    # A redelivery of the same content does not duplicate chunks (content_sha dedup).
    await process_message(deps, _msg("doc-4", "kb-1"))
    chunks_after_second = len([c for c in db.chunks if c["doc_id"] == "doc-4"])
    assert chunks_after_first == chunks_after_second
