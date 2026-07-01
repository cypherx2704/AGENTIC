"""PgVectorAdapter — the first-cycle IVectorStore backed by Postgres + pgvector.

Implements:
  * ``upsert``         — batched multi-row INSERT into ``rag.chunks`` + ``rag.chunk_vectors_1536``
    with worker-side ``(doc_id, content_sha256)`` dedup (re-enqueued ingestion skips
    already-stored content — the Valkey-outage defence in depth).
  * ``search``         — the two-pass CTE (HNSW-friendly ORDER BY, post-fetch score floor),
    ``SET LOCAL hnsw.ef_search`` per query.
  * ``delete_document``— DB cascade (chunks + vectors ON DELETE CASCADE off documents).
  * ``estimate_size``  — chunk count + a ~24 KiB/chunk at-rest estimate.

The pgvector ``vector`` literal is sent as the canonical ``[f1,f2,...]`` text form so no
extra driver adapter registration is needed.

The adapter resolver returns this for every tenant in first cycle and write-through-creates
a missing ``rag.tenant_backends`` row on first touch (no per-tenant bootstrap consumer).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ...core.config import Settings
from .base import ChunkHit, ChunkVector, StorageStats

logger = structlog.get_logger(__name__)

# Per-dimension vector table map. Only 1536 ships first cycle; new dims add a row + table.
_VECTOR_TABLE = {1536: "rag.chunk_vectors_1536"}

# At-rest bytes per 1536-dim chunk incl. HNSW overhead (~24 KiB) — the storage-quota driver.
_BYTES_PER_CHUNK = 24 * 1024


def _vector_table_for(dim: int) -> str:
    table = _VECTOR_TABLE.get(dim)
    if table is None:
        raise ValueError(f"no vector table for embedding_dim={dim} (only 1536 ships first cycle)")
    return table


def _vector_literal(embedding: list[float]) -> str:
    """pgvector text input form: [f1,f2,...]."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _content_sha(doc_id: str, content: str) -> str:
    return hashlib.sha256(f"{doc_id}|{content}".encode()).hexdigest()


class PgVectorAdapter:
    """IVectorStore over Postgres + pgvector."""

    def __init__(self, pool: AsyncConnectionPool, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings

    async def upsert(self, tenant_id: str, chunks: list[ChunkVector]) -> int:
        if not chunks:
            return 0
        from ...db.pool import in_tenant

        async def _txn(conn: AsyncConnection) -> int:
            # Dedup: which (doc_id, content_sha) already exist? content_sha lives in
            # chunks.metadata->>'content_sha' so a re-enqueued job skips re-insert.
            doc_id = chunks[0].doc_id
            existing = await self._existing_shas(conn, doc_id)
            inserted = 0
            for cv in chunks:
                sha = _content_sha(cv.doc_id, cv.content)
                if sha in existing:
                    continue
                meta = dict(cv.metadata)
                meta["content_sha"] = sha
                cur = await conn.execute(
                    """
                    INSERT INTO rag.chunks
                      (doc_id, kb_id, tenant_id, content, chunk_index,
                       embedding_model, embedding_dim, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING chunk_id
                    """,
                    (
                        cv.doc_id, cv.kb_id, tenant_id, cv.content, cv.chunk_index,
                        cv.embedding_model, cv.embedding_dim, json.dumps(meta),
                    ),
                )
                row = await cur.fetchone()
                chunk_id = row[0]
                table = _vector_table_for(cv.embedding_dim)
                await conn.execute(
                    f"INSERT INTO {table} (chunk_id, tenant_id, kb_id, embedding) "  # noqa: S608 — table from a fixed map
                    "VALUES (%s,%s,%s,%s)",
                    (chunk_id, tenant_id, cv.kb_id, _vector_literal(cv.embedding)),
                )
                existing.add(sha)
                inserted += 1
            return inserted

        return await in_tenant(self._pool, tenant_id, _txn)

    @staticmethod
    async def _existing_shas(conn: AsyncConnection, doc_id: str) -> set[str]:
        cur = await conn.execute(
            "SELECT metadata->>'content_sha' AS sha FROM rag.chunks WHERE doc_id = %s",
            (doc_id,),
        )
        rows = await cur.fetchall()
        return {r[0] for r in rows if r[0]}

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
        from ...db.pool import in_tenant

        table = _vector_table_for(dimension)
        vec = _vector_literal(embedding)
        filters_json = json.dumps(filters) if filters else None

        async def _txn(conn: AsyncConnection) -> list[ChunkHit]:
            # Per-query HNSW recall/latency knob (transaction-local).
            # ef_search is a server-clamped int; SET LOCAL forbids bind params, so interpolate the int literal.
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
            cur = conn.cursor(row_factory=dict_row)
            # Two-pass CTE: candidate ORDER BY stays index-friendly; score floor post-fetch.
            sql = f"""
                WITH candidates AS (
                  SELECT c.chunk_id, c.content, c.metadata, c.doc_id,
                         cv.embedding <=> %(vec)s::vector AS distance
                  FROM {table} cv
                  JOIN rag.chunks c USING (chunk_id)
                  WHERE c.kb_id = %(kb_id)s
                    AND (%(filters)s::jsonb IS NULL OR c.metadata @> %(filters)s::jsonb)
                  ORDER BY cv.embedding <=> %(vec)s::vector
                  LIMIT %(buffer)s
                )
                SELECT chunk_id, content, metadata, doc_id, 1 - distance AS score
                FROM candidates
                WHERE 1 - distance >= %(min_score)s
                ORDER BY distance
                LIMIT %(top_k)s
            """  # noqa: S608 — `table` is from the fixed _VECTOR_TABLE map, never user input
            await cur.execute(
                sql,
                {
                    "vec": vec,
                    "kb_id": kb_id,
                    "filters": filters_json,
                    "buffer": top_k * 2,
                    "min_score": min_score,
                    "top_k": top_k,
                },
            )
            rows = await cur.fetchall()
            return [
                ChunkHit(
                    chunk_id=str(r["chunk_id"]),
                    doc_id=str(r["doc_id"]),
                    content=r["content"],
                    score=float(r["score"]),
                    metadata=r["metadata"] or {},
                )
                for r in rows
            ]

        return await in_tenant(self._pool, tenant_id, _txn)

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
        """Postgres-native hybrid retrieval: a dense (pgvector) leg + a lexical
        (websearch_to_tsquery + ts_rank_cd) leg fused with Reciprocal Rank Fusion in SQL.

        RRF score per chunk = sum over legs of ``1 / (rrf_k + rank_in_leg)``. This is robust
        to the two legs' incomparable score scales (cosine vs ts_rank). ``mode``:
          * ``hybrid`` — both legs fused (default for search_mode='hybrid').
          * ``sparse`` — lexical leg only (search_mode='sparse').

        The dense leg keeps the index-friendly ``ORDER BY embedding <=> vec`` shape (no score
        floor pushed into the candidate WHERE) exactly like the two-pass dense path. The
        lexical leg is GIN-index-friendly via ``content_tsv @@ websearch_to_tsquery(...)``.
        The returned ``score`` is the fused RRF score (NOT a cosine similarity), so callers
        must not apply a cosine min_score floor to it.
        """
        from ...db.pool import in_tenant

        table = _vector_table_for(dimension)
        filters_json = json.dumps(filters) if filters else None
        use_dense = mode != "sparse" and embedding is not None
        vec = _vector_literal(embedding) if use_dense else None

        async def _txn(conn: AsyncConnection) -> list[ChunkHit]:
            cur = conn.cursor(row_factory=dict_row)
            params: dict[str, Any] = {
                "kb_id": kb_id,
                "filters": filters_json,
                "candidates": candidates,
                "rrf_k": rrf_k,
                "top_k": top_k,
                "qtext": query_text,
            }
            # Lexical leg: rank by ts_rank_cd over the generated content_tsv (migration 0003).
            lexical_cte = """
                lexical AS (
                  SELECT c.chunk_id,
                         ROW_NUMBER() OVER (
                           ORDER BY ts_rank_cd(c.content_tsv, q) DESC, c.chunk_id
                         ) AS rnk
                  FROM rag.chunks c, websearch_to_tsquery('english', %(qtext)s) AS q
                  WHERE c.kb_id = %(kb_id)s
                    AND (%(filters)s::jsonb IS NULL OR c.metadata @> %(filters)s::jsonb)
                    AND c.content_tsv @@ q
                  ORDER BY ts_rank_cd(c.content_tsv, q) DESC, c.chunk_id
                  LIMIT %(candidates)s
                )
            """
            if use_dense:
                # ef_search is a server-clamped int; SET LOCAL forbids bind params, so interpolate the int literal.
                await conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
                params["vec"] = vec
                # Dense leg: index-friendly ORDER BY embedding <=> vec; rank candidates.
                dense_cte = f"""
                    dense AS (
                      SELECT c.chunk_id,
                             ROW_NUMBER() OVER (ORDER BY cv.embedding <=> %(vec)s::vector) AS rnk
                      FROM {table} cv
                      JOIN rag.chunks c USING (chunk_id)
                      WHERE c.kb_id = %(kb_id)s
                        AND (%(filters)s::jsonb IS NULL OR c.metadata @> %(filters)s::jsonb)
                      ORDER BY cv.embedding <=> %(vec)s::vector
                      LIMIT %(candidates)s
                    )
                """  # noqa: S608 — `table` is from the fixed _VECTOR_TABLE map, never user input
                sql = f"""
                    WITH {dense_cte}, {lexical_cte},
                    fused AS (
                      SELECT chunk_id,
                             SUM(1.0 / (%(rrf_k)s + rnk)) AS score
                      FROM (
                        SELECT chunk_id, rnk FROM dense
                        UNION ALL
                        SELECT chunk_id, rnk FROM lexical
                      ) legs
                      GROUP BY chunk_id
                    )
                    SELECT f.chunk_id, c.content, c.metadata, c.doc_id, f.score
                    FROM fused f
                    JOIN rag.chunks c USING (chunk_id)
                    ORDER BY f.score DESC, f.chunk_id
                    LIMIT %(top_k)s
                """  # noqa: S608 — interpolated CTEs use the fixed-map table only
            else:
                # Sparse-only: RRF over the single lexical leg (degenerates to 1/(k+rnk)).
                sql = f"""
                    WITH {lexical_cte},
                    fused AS (
                      SELECT chunk_id, SUM(1.0 / (%(rrf_k)s + rnk)) AS score
                      FROM lexical
                      GROUP BY chunk_id
                    )
                    SELECT f.chunk_id, c.content, c.metadata, c.doc_id, f.score
                    FROM fused f
                    JOIN rag.chunks c USING (chunk_id)
                    ORDER BY f.score DESC, f.chunk_id
                    LIMIT %(top_k)s
                """
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [
                ChunkHit(
                    chunk_id=str(r["chunk_id"]),
                    doc_id=str(r["doc_id"]),
                    content=r["content"],
                    score=float(r["score"]),
                    metadata=r["metadata"] or {},
                )
                for r in rows
            ]

        return await in_tenant(self._pool, tenant_id, _txn)

    async def delete_document(self, tenant_id: str, doc_id: str) -> None:
        from ...db.pool import in_tenant

        async def _txn(conn: AsyncConnection) -> None:
            # chunks + chunk_vectors_* cascade off documents ON DELETE CASCADE.
            await conn.execute("DELETE FROM rag.documents WHERE doc_id = %s", (doc_id,))

        await in_tenant(self._pool, tenant_id, _txn)

    async def estimate_size(self, tenant_id: str, kb_id: str) -> StorageStats:
        from ...db.pool import in_tenant

        async def _txn(conn: AsyncConnection) -> StorageStats:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM rag.chunks WHERE kb_id = %s", (kb_id,)
            )
            row = await cur.fetchone()
            count = int(row[0]) if row else 0
            return StorageStats(chunk_count=count, estimated_bytes=count * _BYTES_PER_CHUNK)

        return await in_tenant(self._pool, tenant_id, _txn)


async def resolve_vector_store(
    pool: AsyncConnectionPool, settings: Settings, tenant_id: str
) -> PgVectorAdapter:
    """Resolve the per-tenant backend. First cycle: always pgvector; a missing
    ``rag.tenant_backends`` row resolves as pgvector and is write-through-created on
    first touch (no per-tenant bootstrap consumer — Amendment Log)."""
    try:
        from ...db.pool import in_tenant

        async def _ensure(conn: AsyncConnection) -> None:
            await conn.execute(
                """
                INSERT INTO rag.tenant_backends (tenant_id, backend_type)
                VALUES (%s, 'pgvector')
                ON CONFLICT (tenant_id) DO NOTHING
                """,
                (tenant_id,),
            )

        await in_tenant(pool, tenant_id, _ensure)
    except Exception as exc:  # noqa: BLE001 — write-through is best-effort; default is pgvector
        logger.warning("tenant_backend_writethrough_failed", error=str(exc))
    return PgVectorAdapter(pool, settings)
