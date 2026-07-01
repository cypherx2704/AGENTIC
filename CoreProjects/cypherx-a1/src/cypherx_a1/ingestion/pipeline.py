"""Ingestion pipeline: landing -> normalization (graph) -> RAG ingest -> citation.

For each canonical record:

1. **Land** the raw event idempotently (``raw_events`` unique on source+external_id+
   content_sha). A duplicate short-circuits the record (no re-processing, no re-embed).
2. **Normalize** nodes + edges into the app-owned graph (one ``in_tenant`` tx) and capture
   the node→entity_id map.
3. **RAG ingest** each doc into the resolved per-tenant KB (an HTTP call OUTSIDE any DB tx),
   then record the ``vector_ref`` on the node and a ``doc_id``-keyed citation (a second tx)
   and emit a ``cypherx.cypherxa1.record.normalized`` event via the outbox.

The KB binding is resolved + persisted once per (tenant, logical KB) with the embedding
model pinned + immutable (the Phase-alignment guarantee). The graph never enters RAG, and
RAG holds only opaque text + provenance metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..core import metrics, trace
from ..core.config import Settings
from ..db import graph_repo, ingest_repo
from ..db.outbox import enqueue_event
from ..db.pool import in_tenant
from ..models.canonical import CanonicalRecord, NodeRef
from ..services.rag_client import RagClient
from .normalizer import upsert_graph

logger = structlog.get_logger(__name__)

TOPIC_RECORD_NORMALIZED = "cypherx.cypherxa1.record.normalized"


@dataclass
class IngestStats:
    records_seen: int = 0
    records_new: int = 0
    nodes_upserted: int = 0
    edges_upserted: int = 0
    docs_ingested: int = 0
    skipped_duplicate: int = 0
    errors: int = 0
    sources: set[str] = field(default_factory=set)


class KbResolver:
    """Resolves a logical KB name to a RAG ``kb_id`` per tenant (create-once, then cached).

    The resolved embedding model + dim are persisted immutably in ``cypherx_a1.rag_kbs`` so
    every KB shares one stable vector space; an in-process cache avoids a DB hit per doc."""

    def __init__(self, settings: Settings, rag: RagClient) -> None:
        self._settings = settings
        self._rag = rag
        self._cache: dict[tuple[str, str], str] = {}  # (tenant_id, logical) -> kb_id

    async def resolve(
        self,
        pool: AsyncConnectionPool,
        *,
        tenant_id: str,
        logical: str,
        agent_jwt: str,
        on_behalf_of: str | None,
    ) -> str:
        key = (tenant_id, logical)
        if key in self._cache:
            return self._cache[key]

        async def _read(conn: AsyncConnection) -> dict | None:
            return await ingest_repo.get_rag_kb(conn, logical_name=logical)

        existing = await in_tenant(pool, tenant_id, _read)
        if existing:
            self._cache[key] = existing["kb_id"]
            return existing["kb_id"]

        kb_name = f"cypherx-a1::{logical}"
        info = await self._rag.create_kb(name=kb_name, agent_jwt=agent_jwt, on_behalf_of=on_behalf_of)

        async def _persist(conn: AsyncConnection) -> dict | None:
            await ingest_repo.set_rag_kb(
                conn,
                logical_name=logical,
                kb_id=info.kb_id,
                model=info.embedding_model_resolved or self._settings.rag_embedding_model,
                dim=info.embedding_dim,
            )
            return await ingest_repo.get_rag_kb(conn, logical_name=logical)

        winner = await in_tenant(pool, tenant_id, _persist)
        kb_id = winner["kb_id"] if winner else info.kb_id
        self._cache[key] = kb_id
        return kb_id


async def ingest_records(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_jwt: str | None,
    agent_id: str | None,
    records: list[CanonicalRecord],
    rag: RagClient | None,
    kb_resolver: KbResolver | None,
    producer_version: str,
    settings: Settings | None = None,
) -> IngestStats:
    """Ingest a batch. When ``rag``/``agent_jwt`` are None (e.g. the webhook path, which has
    no inbound agent JWT to forward) the docs are NOT embedded — only landing + graph
    normalization run, and RAG enrichment is deferred to an authenticated sync / worker.

    ``settings`` (optional) is threaded to the normalizer to enable the opt-in entity-
    resolution pass; when None or the flag is off, normalization is exactly today's."""
    stats = IngestStats()
    for record in records:
        stats.records_seen += 1
        stats.sources.add(record.source)
        try:
            await _ingest_one(
                pool,
                tenant_id=tenant_id,
                agent_jwt=agent_jwt,
                agent_id=agent_id,
                record=record,
                rag=rag,
                kb_resolver=kb_resolver,
                producer_version=producer_version,
                stats=stats,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 — one bad record must not abort a backfill
            stats.errors += 1
            logger.warning("ingest_record_failed", external_id=record.external_id, error=str(exc))
    return stats


async def _ingest_one(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    agent_jwt: str | None,
    agent_id: str | None,
    record: CanonicalRecord,
    rag: RagClient | None,
    kb_resolver: KbResolver | None,
    producer_version: str,
    stats: IngestStats,
    settings: Settings | None = None,
) -> None:
    # 1) Land + 2) normalize in one tenant tx; capture node ids for the docs.
    async def _land_and_normalize(conn: AsyncConnection) -> dict[NodeRef, str] | None:
        is_new = await ingest_repo.record_raw_event(
            conn,
            source=record.source,
            external_id=record.external_id,
            record_type=record.record_type,
            content_sha=record.content_sha,
            payload=record.raw_payload or None,
        )
        if not is_new:
            return None
        result = await upsert_graph(conn, record, settings=settings)
        stats.nodes_upserted += len(result.node_ids)
        stats.edges_upserted += result.edges_upserted
        for rel in {e.rel for e in record.edges}:
            metrics.graph_edges_upserted_total.labels(rel).inc()
        return result.node_ids

    node_ids = await in_tenant(pool, tenant_id, _land_and_normalize)
    if node_ids is None:
        stats.skipped_duplicate += 1
        return
    stats.records_new += 1
    metrics.ingestion_records_total.labels(record.source, "new").inc()

    # 3) RAG ingest each doc (HTTP, outside any tx), then record vector_ref + citation + event.
    # Skipped on the webhook path (no agent JWT to forward) — RAG enrichment is deferred.
    if rag is None or kb_resolver is None or not agent_jwt:
        return
    trace_id = trace.trace_id_var.get()
    for doc in record.docs:
        entity_id = node_ids.get(doc.node)
        if entity_id is None:
            continue
        kb_id = await kb_resolver.resolve(
            pool, tenant_id=tenant_id, logical=doc.kb, agent_jwt=agent_jwt, on_behalf_of=agent_id
        )
        metadata = {**doc.metadata, "node_id": entity_id, "kb": doc.kb}
        ingested = await rag.ingest_inline(
            kb_id=kb_id,
            name=doc.name,
            content=doc.content,
            source_type=doc.source_type,
            metadata=metadata,
            agent_jwt=agent_jwt,
            on_behalf_of=agent_id,
            idempotency_key=f"{tenant_id}:{record.content_sha}:{doc.kb}",
        )

        async def _link(
            conn: AsyncConnection,
            _kb_id: str = kb_id,
            _doc_id: str = ingested.doc_id,
            _eid: str = entity_id,
        ) -> None:
            await graph_repo.set_vector_ref(conn, entity_id=_eid, vector_ref={"kb_id": _kb_id, "doc_id": _doc_id})
            await ingest_repo.add_citation(conn, kb_id=_kb_id, doc_id=_doc_id, chunk_id=None, entity_id=_eid)
            await enqueue_event(
                conn,
                topic=TOPIC_RECORD_NORMALIZED,
                tenant_id=tenant_id,
                trace_id=trace_id,
                event_type="cypherx.cypherxa1.record.normalized",
                payload={
                    "source": record.source,
                    "external_id": record.external_id,
                    "kb_id": _kb_id,
                    "doc_id": _doc_id,
                    "entity_id": _eid,
                },
                producer_version=producer_version,
            )

        await in_tenant(pool, tenant_id, _link)
        stats.docs_ingested += 1
