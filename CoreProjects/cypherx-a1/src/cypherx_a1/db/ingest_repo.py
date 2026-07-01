"""Ingestion-side data access: raw landing, connectors, cursors, extraction ledger,
RAG-KB bindings, and citations. Like :mod:`graph_repo`, every function runs on a
connection already inside an ``in_tenant`` transaction (RLS-scoped)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


async def record_raw_event(
    conn: AsyncConnection,
    *,
    source: str,
    external_id: str,
    record_type: str,
    content_sha: str,
    payload: dict[str, Any] | None,
) -> bool:
    """Land a raw event idempotently. Returns True if newly inserted, False if a duplicate
    (same source+external_id+content_sha already landed) — the caller skips re-processing."""
    cur = await conn.execute(
        """
        INSERT INTO cypherx_a1.raw_events
            (tenant_id, source, external_id, record_type, content_sha, payload)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, source, external_id, content_sha) DO NOTHING
        """,
        (source, external_id, record_type, content_sha, Jsonb(payload) if payload is not None else None),
    )
    return cur.rowcount > 0


async def get_or_create_connector(
    conn: AsyncConnection, *, kind: str, display_name: str, config: dict[str, Any] | None = None
) -> str:
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        INSERT INTO cypherx_a1.connectors (tenant_id, kind, display_name, config)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s)
        ON CONFLICT (tenant_id, kind, display_name)
        DO UPDATE SET config = cypherx_a1.connectors.config || EXCLUDED.config, updated_at = NOW()
        RETURNING connector_id
        """,
        (kind, display_name, Jsonb(config or {})),
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row["connector_id"])


async def get_cursor(conn: AsyncConnection, *, connector_id: str, stream: str) -> str | None:
    cur = await conn.cursor(row_factory=dict_row).execute(
        "SELECT cursor FROM cypherx_a1.sync_cursors WHERE connector_id = %s AND stream = %s",
        (connector_id, stream),
    )
    row = await cur.fetchone()
    return row["cursor"] if row else None


async def set_cursor(conn: AsyncConnection, *, connector_id: str, stream: str, cursor: str) -> None:
    await conn.execute(
        """
        INSERT INTO cypherx_a1.sync_cursors (tenant_id, connector_id, stream, cursor)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s)
        ON CONFLICT (tenant_id, connector_id, stream)
        DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = NOW()
        """,
        (connector_id, stream, cursor),
    )


async def extraction_job_done(
    conn: AsyncConnection, *, node_id: str, content_sha: str, extractor_version: str
) -> bool:
    cur = await conn.execute(
        """
        SELECT 1 FROM cypherx_a1.extraction_jobs
         WHERE node_id = %s AND content_sha = %s AND extractor_version = %s AND status = 'completed'
        """,
        (node_id, content_sha, extractor_version),
    )
    return (await cur.fetchone()) is not None


async def record_extraction_job(
    conn: AsyncConnection,
    *,
    node_id: str,
    content_sha: str,
    extractor_version: str,
    edges_extracted: int,
    llm_call_id: str | None,
    cost_usd: float,
    status: str = "completed",
) -> None:
    await conn.execute(
        """
        INSERT INTO cypherx_a1.extraction_jobs
            (tenant_id, node_id, content_sha, extractor_version, status, edges_extracted, llm_call_id, cost_usd)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, node_id, content_sha, extractor_version)
        DO UPDATE SET status = EXCLUDED.status, edges_extracted = EXCLUDED.edges_extracted,
                      llm_call_id = EXCLUDED.llm_call_id, cost_usd = EXCLUDED.cost_usd
        """,
        (node_id, content_sha, extractor_version, status, edges_extracted, llm_call_id, cost_usd),
    )


async def get_rag_kb(conn: AsyncConnection, *, logical_name: str) -> dict[str, Any] | None:
    cur = await conn.cursor(row_factory=dict_row).execute(
        "SELECT kb_id, embedding_model_resolved, embedding_dim FROM cypherx_a1.rag_kbs WHERE logical_name = %s",
        (logical_name,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_rag_kb(
    conn: AsyncConnection, *, logical_name: str, kb_id: str, model: str, dim: int
) -> None:
    await conn.execute(
        """
        INSERT INTO cypherx_a1.rag_kbs (tenant_id, logical_name, kb_id, embedding_model_resolved, embedding_dim)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, logical_name) DO NOTHING
        """,
        (logical_name, kb_id, model, dim),
    )


async def add_citation(
    conn: AsyncConnection,
    *,
    kb_id: str,
    doc_id: str | None,
    chunk_id: str | None,
    entity_id: str | None,
    edge_id: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO cypherx_a1.citations (tenant_id, kb_id, doc_id, chunk_id, entity_id, edge_id)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s, %s)
        """,
        (kb_id, doc_id, chunk_id, entity_id, edge_id),
    )


async def entities_for_chunks(
    conn: AsyncConnection, *, chunk_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Map RAG chunk_id -> its originating entity (for citation back-mapping)."""
    if not chunk_ids:
        return {}
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT c.chunk_id, e.entity_id, e.kind, e.natural_key, e.title
          FROM cypherx_a1.citations c
          JOIN cypherx_a1.entities e ON e.entity_id = c.entity_id AND e.valid_to IS NULL
         WHERE c.chunk_id = ANY(%s)
        """,
        (chunk_ids,),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in await cur.fetchall():
        out[str(r["chunk_id"])] = dict(r)
    return out


async def entities_for_docs(
    conn: AsyncConnection, *, doc_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Map RAG doc_id -> its originating entity. doc_id is the stable citation key: each
    RagDoc corresponds to exactly one graph node, recorded at ingest time."""
    if not doc_ids:
        return {}
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT DISTINCT ON (c.doc_id) c.doc_id, e.entity_id, e.kind, e.natural_key, e.title
          FROM cypherx_a1.citations c
          JOIN cypherx_a1.entities e ON e.entity_id = c.entity_id AND e.valid_to IS NULL
         WHERE c.doc_id = ANY(%s)
        """,
        (doc_ids,),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in await cur.fetchall():
        out[str(r["doc_id"])] = dict(r)
    return out


async def list_unextracted_entities(
    conn: AsyncConnection, *, extractor_version: str, limit: int = 100
) -> list[dict[str, Any]]:
    """Current entities that have a content_sha but no completed extraction job at the
    current extractor_version (drives the extraction pass)."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT e.entity_id, e.kind, e.natural_key, e.title, e.search_text, e.content_sha
          FROM cypherx_a1.entities e
         WHERE e.valid_to IS NULL AND e.content_sha IS NOT NULL
           AND e.kind IN ('pr','ticket','incident','decision','document')
           AND NOT EXISTS (
               SELECT 1 FROM cypherx_a1.extraction_jobs j
                WHERE j.node_id = e.entity_id AND j.content_sha = e.content_sha
                  AND j.extractor_version = %s AND j.status = 'completed')
         LIMIT %s
        """,
        (extractor_version, limit),
    )
    return [dict(r) for r in await cur.fetchall()]
