"""Shared ingestion pipeline: chunk -> batched-embed -> batched vector INSERT.

Used by BOTH the inline-ingest endpoint (synchronous, small payloads) and the Kafka
ingestion worker (asynchronous, presigned-upload payloads). Centralising it keeps the
chunking + batch-embedding + dedup logic in one place.

Embedding batches are bounded (≤ ``embed_batch_max_items`` AND ≤ ``embed_batch_max_bytes``,
whichever first) with deterministic ``Idempotency-Key: embed:{doc_id}:{first_chunk_index}``
so a worker crash + restart never double-bills. Chunk order (``chunk_index`` ascending) is
preserved so reassembly is deterministic + resumable. Vectors are written via the
PgVectorAdapter's batched upsert (which also applies the ``(doc_id, content_sha)`` dedup).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from ..core.config import Settings
from .chunking import chunk_text
from .contextual import Contextualizer
from .embeddings import EmbeddingClient
from .store.base import ChunkVector, IVectorStore

logger = structlog.get_logger(__name__)


@dataclass
class IngestResult:
    chunks_indexed: int
    embedding_tokens_used: int
    embedding_source: str


def _batches(
    chunk_count_texts: list[str], *, max_items: int, max_bytes: int
) -> list[tuple[int, int]]:
    """Return (start, end) index ranges honouring the item + byte caps."""
    ranges: list[tuple[int, int]] = []
    start = 0
    cur_bytes = 0
    for i, text in enumerate(chunk_count_texts):
        size = len(text.encode("utf-8"))
        if i > start and (i - start >= max_items or cur_bytes + size > max_bytes):
            ranges.append((start, i))
            start = i
            cur_bytes = 0
        cur_bytes += size
    if start < len(chunk_count_texts):
        ranges.append((start, len(chunk_count_texts)))
    return ranges


async def ingest_text(
    *,
    text: str,
    doc_id: str,
    kb_id: str,
    tenant_id: str,
    embedding_model: str,
    embedding_dim: int,
    chunking_strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    doc_name: str,
    source_uri: str | None,
    embedder: EmbeddingClient,
    store: IVectorStore,
    settings: Settings,
    agent_jwt: str | None = None,
    on_behalf_of: str | None = None,
    contextualizer: Contextualizer | None = None,
    doc_metadata: dict[str, Any] | None = None,
) -> IngestResult:
    """Chunk + embed + store ``text`` for one document. Returns the indexing result.

    When ``settings.rag_contextual_ingest`` is on AND a ``contextualizer`` is provided, each
    chunk gets a 1-2 sentence situating context (generated from the document) that is
    prepended before embedding AND stored in ``metadata['context']`` (folded into the lexical
    ``content_tsv`` by migration 0003). DEFAULT (flag off / no contextualizer): unchanged.

    ``doc_metadata`` (the document-level user metadata, e.g. ``InlineIngestRequest.metadata``)
    is copied onto EVERY chunk's metadata so query ``filters`` (jsonb ``@>`` containment on
    ``chunks.metadata``) match user keys (BUG 4). System keys (``doc_name``/``source_uri``/
    ``context``/``content_sha``) always win over any user key of the same name.
    """
    chunks = chunk_text(
        text, strategy=chunking_strategy, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    if not chunks:
        return IngestResult(chunks_indexed=0, embedding_tokens_used=0, embedding_source="none")

    use_context = settings.rag_contextual_ingest and contextualizer is not None
    # Per-chunk situating context (only when flagged on). Generated against the FULL doc text.
    contexts: list[str] = [""] * len(chunks)
    if use_context:
        from ..core import metrics

        for i, chunk in enumerate(chunks):
            ctx, ctx_source = await contextualizer.contextualize(
                text, chunk.content, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of
            )
            contexts[i] = ctx
            metrics.contextual_ingest_total.labels(ctx_source).inc()

    # Embed text = context + chunk when contextual ingest produced a non-empty context.
    embed_texts = [
        (f"{contexts[i]}\n\n{c.content}" if contexts[i] else c.content)
        for i, c in enumerate(chunks)
    ]
    total_tokens = 0
    source = "mock"
    indexed = 0

    for start, end in _batches(
        embed_texts,
        max_items=settings.embed_batch_max_items,
        max_bytes=settings.embed_batch_max_bytes,
    ):
        batch_chunks = chunks[start:end]
        batch_texts = embed_texts[start:end]
        idem_key = f"embed:{doc_id}:{batch_chunks[0].chunk_index}"
        result = await embedder.embed(
            batch_texts,
            model=embedding_model,
            dim=embedding_dim,
            agent_jwt=agent_jwt,
            on_behalf_of=on_behalf_of,
            idempotency_key=idem_key,
        )
        total_tokens += result.prompt_tokens
        source = result.source
        vectors = []
        for offset, (chunk, vec) in enumerate(zip(batch_chunks, result.vectors, strict=True)):
            # User document metadata first so query filters match; system keys win on collision.
            meta: dict = dict(doc_metadata or {})
            meta["doc_name"] = doc_name
            meta["source_uri"] = source_uri
            ctx = contexts[start + offset]
            if ctx:
                meta["context"] = ctx
            vectors.append(
                ChunkVector(
                    chunk_id="",  # assigned by the DB
                    doc_id=doc_id,
                    kb_id=kb_id,
                    content=chunk.content,
                    chunk_index=chunk.chunk_index,
                    embedding=vec,
                    embedding_model=result.model,
                    embedding_dim=embedding_dim,
                    metadata=meta,
                )
            )
        indexed += await store.upsert(tenant_id, vectors)

    logger.info(
        "document_ingested", doc_id=doc_id, chunks_indexed=indexed, embedding_source=source
    )
    return IngestResult(
        chunks_indexed=indexed, embedding_tokens_used=total_tokens, embedding_source=source
    )
