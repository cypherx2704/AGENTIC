"""IVectorStore — the swappable vector-store interface (Component 5e, ⚡).

The interface is first-cycle; only ``PgVectorAdapter`` is implemented. Designed up-front
so a Pinecone/Qdrant/Weaviate backend is a new adapter class + a per-tenant
``rag.tenant_backends`` row, never a query-layer rewrite. All methods are tenant-scoped;
the adapter is responsible for enforcing the per-request RLS context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ChunkVector:
    """A chunk + its embedding, ready to upsert."""

    chunk_id: str
    doc_id: str
    kb_id: str
    content: str
    chunk_index: int
    embedding: list[float]
    embedding_model: str
    embedding_dim: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkHit:
    """One retrieval result."""

    chunk_id: str
    doc_id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StorageStats:
    chunk_count: int
    estimated_bytes: int


class IVectorStore(Protocol):
    """Every backend implements this. pgvector is the only first-cycle impl."""

    async def upsert(self, tenant_id: str, chunks: list[ChunkVector]) -> int:
        """Insert chunk rows + vector rows; returns the number newly inserted."""
        ...

    async def search(
        self,
        tenant_id: str,
        kb_id: str,
        embedding: list[float],
        *,
        top_k: int,
        min_score: float,
        filters: dict[str, Any] | None,
        dimension: int,
        ef_search: int,
    ) -> list[ChunkHit]:
        """Two-pass vector search scoped to a KB (RLS gates tenant)."""
        ...

    async def search_hybrid(
        self,
        tenant_id: str,
        kb_id: str,
        embedding: list[float] | None,
        query_text: str,
        *,
        top_k: int,
        candidates: int,
        rrf_k: int,
        filters: dict[str, Any] | None,
        dimension: int,
        ef_search: int,
        mode: str = "hybrid",
    ) -> list[ChunkHit]:
        """Hybrid (dense + lexical) / sparse retrieval fused with Reciprocal Rank Fusion.

        ADDITIVE: the default 'dense' path uses ``search`` and is unaffected. Returns hits
        whose ``score`` is the fused RRF score (not a cosine similarity)."""
        ...

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        """Delete all chunks/vectors for a document (DB cascade)."""
        ...

    async def estimate_size(self, tenant_id: str, kb_id: str) -> StorageStats:
        """Return chunk count + an at-rest byte estimate for a KB."""
        ...
