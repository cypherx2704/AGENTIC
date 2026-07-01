"""Normalization: turn a canonical record's nodes + edges into the app-owned graph.

Upserts every node to a stable ``entity_id``, records cross-tool identity handles for
person nodes, then wires the edges — resolving each :class:`NodeRef` to an ``entity_id``
(creating a minimal stub entity for a ref that names a node not present in this record, so
an edge can always be created). Runs on a connection already inside an ``in_tenant`` tx.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from ..core.config import Settings
from ..db import graph_repo
from ..models.canonical import CanonicalNode, CanonicalRecord, NodeRef
from .resolver import resolve_entity


@dataclass
class GraphUpsert:
    """The entity_id map + counters produced by normalizing one record."""

    node_ids: dict[NodeRef, str] = field(default_factory=dict)
    edges_upserted: int = 0


async def _record_identities(conn: AsyncConnection, *, person_entity_id: str, node: CanonicalNode) -> None:
    for source, handle in node.identity_handles:
        await conn.execute(
            """
            INSERT INTO cypherx_a1.identities (tenant_id, person_entity_id, source, handle)
            VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s)
            ON CONFLICT (tenant_id, source, handle) DO NOTHING
            """,
            (person_entity_id, source, handle),
        )


async def _resolve_person_by_handle(conn: AsyncConnection, node: CanonicalNode) -> str | None:
    """Cross-tool identity resolution: if any of this person's handles already maps to a
    canonical person entity, reuse it (so one human isn't split across tools)."""
    for source, handle in node.identity_handles:
        cur = await conn.cursor(row_factory=dict_row).execute(
            "SELECT person_entity_id FROM cypherx_a1.identities WHERE source = %s AND handle = %s",
            (source, handle),
        )
        row = await cur.fetchone()
        if row:
            return str(row["person_entity_id"])
    return None


async def _maybe_resolve(
    conn: AsyncConnection, settings: Settings | None, entity_id: str, node: CanonicalNode
) -> str:
    """Run the (opt-in) coreference resolver for one node; return its canonical id. A no-op
    that returns ``entity_id`` unchanged when resolution is disabled / settings is None."""
    if settings is None or not settings.entity_resolution_enabled:
        return entity_id
    surface = node.title or node.natural_key
    return await resolve_entity(
        conn, settings=settings, entity_id=entity_id, kind=node.kind, surface_form=surface
    )


async def upsert_graph(
    conn: AsyncConnection, record: CanonicalRecord, *, settings: Settings | None = None
) -> GraphUpsert:
    """Normalize a record's nodes + edges into the graph.

    Phase KG (additive, opt-in): when ``settings`` is supplied AND
    ``entity_resolution_enabled`` is set, each upserted node is run through the type-aware
    coreference resolver (``ingestion.resolver``) so duplicate surface forms collapse to one
    canonical entity. With ``settings=None`` (e.g. the live demo) or the flag off, behavior
    is exactly today's — exact handle/email cross-tool identity resolution only."""
    out = GraphUpsert()

    # 1) Upsert nodes -> entity_id map (person nodes resolve cross-tool identity first).
    for node in record.nodes:
        if node.kind == "person":
            existing = await _resolve_person_by_handle(conn, node)
            entity_id = await graph_repo.upsert_entity(
                conn,
                kind=node.kind, source=node.source, natural_key=node.natural_key,
                title=node.title, search_text=node.search_text, external_id=node.external_id,
                attrs=node.attrs, content_sha=None,
            )
            # If a different entity already owned a handle, prefer it as the canonical id and
            # backfill the alias; otherwise register this node's handles.
            canonical = existing or entity_id
            await _record_identities(conn, person_entity_id=canonical, node=node)
            canonical = await _maybe_resolve(conn, settings, canonical, node)
            out.node_ids[node.ref] = canonical
        else:
            entity_id = await graph_repo.upsert_entity(
                conn,
                kind=node.kind, source=node.source, natural_key=node.natural_key,
                title=node.title, search_text=node.search_text, external_id=node.external_id,
                attrs=node.attrs, content_sha=record.content_sha,
            )
            entity_id = await _maybe_resolve(conn, settings, entity_id, node)
            out.node_ids[node.ref] = entity_id

    # 2) Edges — resolve refs (stub-create unknown nodes so edges always wire up).
    for edge in record.edges:
        src_id = await _resolve_ref(conn, edge.src, out)
        dst_id = await _resolve_ref(conn, edge.dst, out)
        await graph_repo.upsert_edge(
            conn,
            src_entity_id=src_id, dst_entity_id=dst_id, rel=edge.rel,
            confidence=edge.confidence, extractor_version="ingest", metadata=edge.metadata,
        )
        out.edges_upserted += 1
    return out


async def _resolve_ref(conn: AsyncConnection, ref: NodeRef, out: GraphUpsert) -> str:
    if ref in out.node_ids:
        return out.node_ids[ref]
    existing = await graph_repo.get_entity_id(conn, kind=ref.kind, natural_key=ref.natural_key)
    if existing:
        out.node_ids[ref] = existing
        return existing
    # Stub: a referenced node we have not seen yet (e.g. a service named only in an edge).
    stub_id = await graph_repo.upsert_entity(
        conn,
        kind=ref.kind, source="derived", natural_key=ref.natural_key,
        title=ref.natural_key, search_text=ref.natural_key, external_id=None,
        attrs={}, content_sha=None,
    )
    out.node_ids[ref] = stub_id
    return stub_id
