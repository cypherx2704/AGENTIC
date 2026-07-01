"""Knowledge-graph data-access layer (app-owned crown jewel).

Adjacency-list entities + typed bitemporal edges in the ``cypherx_a1`` schema, traversed by
recursive CTEs. Every function takes an open ``AsyncConnection`` that is ALREADY inside an
``in_tenant`` transaction (so RLS scopes all reads/writes to the caller's tenant) — the
repo never opens its own transaction or sets ``app.tenant_id`` itself.

Writes (ingestion): :func:`upsert_entity` (stable ``entity_id`` across re-ingest via the
partial unique index on the current slice), :func:`upsert_edge` (supersede-in-place on the
current edge), :func:`set_vector_ref`. Reads (copilot + MCP tools): keyword/FTS search,
one-hop typed neighbors, reverse-``depends_on`` blast radius, owners, and experts.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# ── Relation groups used by the read queries ──────────────────────────────────
OWNERSHIP_RELS = ("owns", "authored", "reviewed", "expert_in")


async def upsert_entity(
    conn: AsyncConnection,
    *,
    kind: str,
    source: str,
    natural_key: str,
    title: str | None,
    search_text: str | None,
    external_id: str | None,
    attrs: dict[str, Any],
    content_sha: str | None,
) -> str:
    """Upsert the CURRENT entity for ``(kind, natural_key)``; return its stable entity_id.

    Conflict target is the partial unique index ``(tenant_id, kind, natural_key) WHERE
    valid_to IS NULL`` so re-ingesting the same node updates in place and keeps the same
    ``entity_id`` (edges + citations stay valid). tenant_id is supplied by RLS context via
    ``current_setting`` so the body never carries identity.
    """
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        INSERT INTO cypherx_a1.entities
            (tenant_id, kind, source, external_id, natural_key, title, search_text, attrs, content_sha)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                %(kind)s, %(source)s, %(external_id)s, %(natural_key)s,
                %(title)s, %(search_text)s, %(attrs)s, %(content_sha)s)
        ON CONFLICT (tenant_id, kind, natural_key) WHERE valid_to IS NULL
        DO UPDATE SET
            title = EXCLUDED.title,
            search_text = EXCLUDED.search_text,
            external_id = COALESCE(EXCLUDED.external_id, cypherx_a1.entities.external_id),
            attrs = cypherx_a1.entities.attrs || EXCLUDED.attrs,
            content_sha = EXCLUDED.content_sha
        RETURNING entity_id
        """,
        {
            "kind": kind,
            "source": source,
            "external_id": external_id,
            "natural_key": natural_key,
            "title": title,
            "search_text": search_text,
            "attrs": Jsonb(attrs or {}),
            "content_sha": content_sha,
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row["entity_id"])


async def get_entity_id(conn: AsyncConnection, *, kind: str, natural_key: str) -> str | None:
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT entity_id FROM cypherx_a1.entities
         WHERE kind = %s AND natural_key = %s AND valid_to IS NULL
         LIMIT 1
        """,
        (kind, natural_key),
    )
    row = await cur.fetchone()
    return str(row["entity_id"]) if row else None


async def set_vector_ref(conn: AsyncConnection, *, entity_id: str, vector_ref: dict[str, Any]) -> None:
    await conn.execute(
        "UPDATE cypherx_a1.entities SET vector_ref = %s WHERE entity_id = %s",
        (Jsonb(vector_ref), entity_id),
    )


# ── Phase KG: entity resolution / canonicalization (mention -> canonical entity) ──────────
async def record_mention(
    conn: AsyncConnection,
    *,
    kind: str,
    surface_form: str,
    normalized_form: str,
    canonical_entity_id: str,
    source: str = "resolver",
    resolver: str = "exact",
    confidence: float = 1.0,
) -> str:
    """Record (idempotently) that a surface form of ``kind`` resolves to a canonical entity.

    Preserves the mention for audit; on a re-observation of the same ``(kind, normalized_form)``
    it points the mention at the latest canonical id. Returns the mention's canonical id."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        INSERT INTO cypherx_a1.entity_mentions
            (tenant_id, kind, surface_form, normalized_form, canonical_entity_id,
             source, resolver, confidence)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                %(kind)s, %(surface)s, %(norm)s, %(canon)s, %(source)s, %(resolver)s, %(conf)s)
        ON CONFLICT (tenant_id, kind, normalized_form)
        DO UPDATE SET canonical_entity_id = EXCLUDED.canonical_entity_id,
                      surface_form = EXCLUDED.surface_form,
                      resolver = EXCLUDED.resolver, confidence = EXCLUDED.confidence
        RETURNING canonical_entity_id
        """,
        {"kind": kind, "surface": surface_form, "norm": normalized_form,
         "canon": canonical_entity_id, "source": source, "resolver": resolver, "conf": confidence},
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row["canonical_entity_id"])


async def lookup_mention(
    conn: AsyncConnection, *, kind: str, normalized_form: str
) -> str | None:
    """Return the canonical entity_id a normalized mention already maps to (if any)."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT m.canonical_entity_id
          FROM cypherx_a1.entity_mentions m
          JOIN cypherx_a1.entities e
            ON e.entity_id = m.canonical_entity_id AND e.valid_to IS NULL
         WHERE m.kind = %s AND m.normalized_form = %s
         LIMIT 1
        """,
        (kind, normalized_form),
    )
    row = await cur.fetchone()
    return str(row["canonical_entity_id"]) if row else None


async def list_current_entities_of_kind(
    conn: AsyncConnection, *, kind: str, limit: int = 1000
) -> list[dict[str, Any]]:
    """Current entities of one kind (for coreference candidate scanning)."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT entity_id, kind, natural_key, title
          FROM cypherx_a1.entities
         WHERE kind = %s AND valid_to IS NULL
         LIMIT %s
        """,
        (kind, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def redirect_edges(
    conn: AsyncConnection, *, from_entity_id: str, to_entity_id: str
) -> int:
    """Redirect all current edges that reference ``from_entity_id`` onto ``to_entity_id``
    (the canonical id) when merging a duplicate entity into its canonical. Self-loops created
    by the redirect are bi-temporally closed. Returns the number of edge endpoints redirected."""
    if from_entity_id == to_entity_id:
        return 0
    cur = await conn.execute(
        "UPDATE cypherx_a1.edges SET src_entity_id = %s "
        " WHERE src_entity_id = %s AND valid_to IS NULL",
        (to_entity_id, from_entity_id),
    )
    moved = cur.rowcount
    cur = await conn.execute(
        "UPDATE cypherx_a1.edges SET dst_entity_id = %s "
        " WHERE dst_entity_id = %s AND valid_to IS NULL",
        (to_entity_id, from_entity_id),
    )
    moved += cur.rowcount
    # Close any self-loop the redirect produced (src == dst) — never a meaningful edge.
    await conn.execute(
        "UPDATE cypherx_a1.edges "
        "   SET valid_to = NOW(), valid_until = NOW(), invalidated_at = NOW() "
        " WHERE src_entity_id = dst_entity_id AND valid_to IS NULL",
    )
    return moved


async def merge_entity(
    conn: AsyncConnection, *, loser_entity_id: str, canonical_entity_id: str
) -> int:
    """Merge a duplicate (``loser``) entity into its ``canonical``: redirect the loser's
    current edges to the canonical id, then bi-temporally close the loser entity (set
    ``valid_to``). The loser row is PRESERVED (not deleted) for audit; the mention map keeps
    pointing surface forms at the canonical id. Returns edge endpoints redirected."""
    moved = await redirect_edges(conn, from_entity_id=loser_entity_id, to_entity_id=canonical_entity_id)
    await conn.execute(
        "UPDATE cypherx_a1.entities SET valid_to = NOW() "
        " WHERE entity_id = %s AND valid_to IS NULL",
        (loser_entity_id,),
    )
    return moved


async def upsert_edge(
    conn: AsyncConnection,
    *,
    src_entity_id: str,
    dst_entity_id: str,
    rel: str,
    confidence: float,
    extractor_version: str,
    evidence_chunk_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Supersede-in-place the CURRENT edge for ``(src, dst, rel)``; return its edge_id.

    There is no natural unique constraint on edges, so this UPDATEs the matching current
    edge (``valid_to IS NULL``) if present, else INSERTs — deterministic + idempotent for
    re-ingest. Extraction supersedes prior versions by ``extractor_version``.
    """
    evidence = evidence_chunk_ids or []
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        UPDATE cypherx_a1.edges
           SET confidence = %(confidence)s,
               extractor_version = %(ev)s,
               evidence_chunk_ids = %(evidence)s::uuid[],
               metadata = %(metadata)s
         WHERE src_entity_id = %(src)s AND dst_entity_id = %(dst)s
           AND rel = %(rel)s AND valid_to IS NULL
        RETURNING edge_id
        """,
        {
            "confidence": confidence,
            "ev": extractor_version,
            "evidence": evidence,
            "metadata": Jsonb(metadata or {}),
            "src": src_entity_id,
            "dst": dst_entity_id,
            "rel": rel,
        },
    )
    row = await cur.fetchone()
    if row is not None:
        return str(row["edge_id"])

    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        INSERT INTO cypherx_a1.edges
            (tenant_id, src_entity_id, dst_entity_id, rel, confidence, extractor_version,
             evidence_chunk_ids, metadata)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                %(src)s, %(dst)s, %(rel)s, %(confidence)s, %(ev)s, %(evidence)s::uuid[], %(metadata)s)
        RETURNING edge_id
        """,
        {
            "src": src_entity_id,
            "dst": dst_entity_id,
            "rel": rel,
            "confidence": confidence,
            "ev": extractor_version,
            "evidence": evidence,
            "metadata": Jsonb(metadata or {}),
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row["edge_id"])


async def upsert_extracted_edge(
    conn: AsyncConnection,
    *,
    src_entity_id: str,
    dst_entity_id: str,
    rel: str,
    confidence: float,
    extractor_version: str,
    metadata: dict[str, Any] | None = None,
    source_span: str | None = None,
    extraction_confidence: float | None = None,
) -> str:
    """Bitemporal upsert for an EXTRACTED edge with an explicit supersede LINK (Phase A).

    If a current edge for ``(src, dst, rel)`` exists and its content (confidence/metadata)
    materially changed, CLOSE it (``valid_to``/``valid_until`` = NOW(), ``invalidated_at`` =
    NOW()) and INSERT a new edge whose ``supersedes_edge_id`` points at the closed one — an
    auditable contradiction chain (Zep/Graphiti bi-temporal). If unchanged, leave it in
    place. If none exists, INSERT fresh.

    Phase KG (additive, opt-in): ``source_span`` + ``extraction_confidence`` record the
    extraction-QA provenance on the new edge; both default to NULL (today's path passes
    neither, so the columns stay NULL and behavior is unchanged).
    """
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT edge_id, confidence, metadata FROM cypherx_a1.edges
         WHERE src_entity_id = %s AND dst_entity_id = %s AND rel = %s AND valid_to IS NULL
         LIMIT 1
        """,
        (src_entity_id, dst_entity_id, rel),
    )
    current = await cur.fetchone()
    meta = metadata or {}
    if current is not None:
        same = abs(float(current["confidence"]) - confidence) < 1e-3 and (current.get("metadata") or {}) == meta
        if same:
            return str(current["edge_id"])
        # Bi-temporal close: valid_to (existing mechanism) + the additive fact-time end
        # (valid_until) + the ingest-time invalidation stamp (invalidated_at) all stamped now.
        await conn.execute(
            "UPDATE cypherx_a1.edges "
            "   SET valid_to = NOW(), valid_until = NOW(), invalidated_at = NOW() "
            " WHERE edge_id = %s",
            (current["edge_id"],),
        )
        supersedes = current["edge_id"]
    else:
        supersedes = None

    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        INSERT INTO cypherx_a1.edges
            (tenant_id, src_entity_id, dst_entity_id, rel, confidence, extractor_version,
             metadata, supersedes_edge_id, source_span, extraction_confidence)
        VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid,
                %(src)s, %(dst)s, %(rel)s, %(confidence)s, %(ev)s, %(metadata)s, %(supersedes)s,
                %(span)s, %(xconf)s)
        RETURNING edge_id
        """,
        {"src": src_entity_id, "dst": dst_entity_id, "rel": rel, "confidence": confidence,
         "ev": extractor_version, "metadata": Jsonb(meta), "supersedes": supersedes,
         "span": source_span, "xconf": extraction_confidence},
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row["edge_id"])


async def supersede_extracted_edges(
    conn: AsyncConnection, *, src_entity_id: str, extractor_version: str
) -> None:
    """Bitemporally close prior extracted edges from a node before a fresh extraction pass.

    Closes the CURRENT extracted edges (``extractor_version <> 'ingest'``) older than the
    new ``extractor_version`` so re-extraction supersedes rather than duplicates.
    """
    await conn.execute(
        """
        UPDATE cypherx_a1.edges
           SET valid_to = NOW(), valid_until = NOW(), invalidated_at = NOW()
         WHERE src_entity_id = %s
           AND valid_to IS NULL
           AND extractor_version <> 'ingest'
           AND extractor_version <> %s
        """,
        (src_entity_id, extractor_version),
    )


# ── Phase KG: bi-temporal reads (history / as-of). Default reads stay on the current ──────
# slice (valid_to IS NULL); these are ADDITIVE — only used when a caller asks for history.
async def edge_history(
    conn: AsyncConnection, *, src_entity_id: str, dst_entity_id: str, rel: str
) -> list[dict[str, Any]]:
    """Full bi-temporal revision chain for ``(src, dst, rel)`` — current + all superseded
    versions, newest valid_from first. Lets a reader reconstruct WHY an answer changed."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT edge_id, rel, confidence, extractor_version, metadata, supersedes_edge_id,
               valid_from, valid_to, valid_until, ingested_at, invalidated_at,
               source_span, extraction_confidence
          FROM cypherx_a1.edges
         WHERE src_entity_id = %s AND dst_entity_id = %s AND rel = %s
         ORDER BY valid_from DESC, ingested_at DESC
        """,
        (src_entity_id, dst_entity_id, rel),
    )
    return [dict(r) for r in await cur.fetchall()]


async def neighbors_as_of(
    conn: AsyncConnection,
    *,
    entity_id: str,
    as_of: str,
    rels: tuple[str, ...] | None = None,
    direction: str = "out",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """One-hop typed neighbours of ``entity_id`` AS OF a timestamp: edges that were valid at
    ``as_of`` (valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)). The
    time-travel counterpart of :func:`neighbors`; default reads still use the current slice."""
    rel_clause = "AND e.rel = ANY(%(rels)s)" if rels else ""
    params: dict[str, Any] = {"eid": entity_id, "limit": limit, "as_of": as_of}
    if rels:
        params["rels"] = list(rels)

    if direction == "out":
        join = "e.src_entity_id = %(eid)s AND n.entity_id = e.dst_entity_id"
    elif direction == "in":
        join = "e.dst_entity_id = %(eid)s AND n.entity_id = e.src_entity_id"
    else:  # both
        join = (
            "((e.src_entity_id = %(eid)s AND n.entity_id = e.dst_entity_id) "
            "OR (e.dst_entity_id = %(eid)s AND n.entity_id = e.src_entity_id))"
        )

    cur = await conn.cursor(row_factory=dict_row).execute(
        f"""
        SELECT n.entity_id, n.kind, n.natural_key, n.title, n.attrs,
               e.rel, e.confidence, e.evidence_chunk_ids, e.edge_id
          FROM cypherx_a1.edges e
          JOIN cypherx_a1.entities n ON {join}
         WHERE e.valid_from <= %(as_of)s::timestamptz
           AND (e.valid_to IS NULL OR e.valid_to > %(as_of)s::timestamptz)
           AND n.valid_from <= %(as_of)s::timestamptz
           AND (n.valid_to IS NULL OR n.valid_to > %(as_of)s::timestamptz)
           {rel_clause}
         ORDER BY e.confidence DESC
         LIMIT %(limit)s
        """,
        params,
    )
    return [dict(r) for r in await cur.fetchall()]


# ── Reads (copilot + MCP query surface) ───────────────────────────────────────
async def find_entities(
    conn: AsyncConnection, *, query: str, kinds: list[str] | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Keyword/FTS search over current entities; ranked by ts_rank. Also matches on an
    exact natural_key (so ``who_owns("owner/name")`` resolves the repo directly)."""
    sql = """
        SELECT entity_id, kind, source, natural_key, title, attrs, vector_ref, created_at,
               COALESCE((SELECT max(ed.confidence) FROM cypherx_a1.edges ed
                          WHERE (ed.src_entity_id = entities.entity_id
                                 OR ed.dst_entity_id = entities.entity_id)
                            AND ed.valid_to IS NULL), 1.0) AS edge_confidence,
               ts_rank(fts, plainto_tsquery('english', %(q)s)) AS rank
          FROM cypherx_a1.entities
         WHERE valid_to IS NULL
           AND (fts @@ plainto_tsquery('english', %(q)s) OR natural_key = %(q)s)
    """
    params: dict[str, Any] = {"q": query, "limit": limit}
    if kinds:
        sql += " AND kind = ANY(%(kinds)s)"
        params["kinds"] = kinds
    sql += " ORDER BY (natural_key = %(q)s) DESC, rank DESC LIMIT %(limit)s"
    cur = await conn.cursor(row_factory=dict_row).execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def get_entity(conn: AsyncConnection, *, entity_id: str) -> dict[str, Any] | None:
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT entity_id, kind, source, natural_key, title, attrs, vector_ref
          FROM cypherx_a1.entities
         WHERE entity_id = %s AND valid_to IS NULL
        """,
        (entity_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def neighbors(
    conn: AsyncConnection,
    *,
    entity_id: str,
    rels: tuple[str, ...] | None = None,
    direction: str = "out",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """One-hop typed neighbours of ``entity_id``. ``direction`` ∈ {out, in, both}."""
    rel_clause = "AND e.rel = ANY(%(rels)s)" if rels else ""
    params: dict[str, Any] = {"eid": entity_id, "limit": limit}
    if rels:
        params["rels"] = list(rels)

    if direction == "out":
        join = "e.src_entity_id = %(eid)s AND n.entity_id = e.dst_entity_id"
    elif direction == "in":
        join = "e.dst_entity_id = %(eid)s AND n.entity_id = e.src_entity_id"
    else:  # both
        join = (
            "((e.src_entity_id = %(eid)s AND n.entity_id = e.dst_entity_id) "
            "OR (e.dst_entity_id = %(eid)s AND n.entity_id = e.src_entity_id))"
        )

    cur = await conn.cursor(row_factory=dict_row).execute(
        f"""
        SELECT n.entity_id, n.kind, n.natural_key, n.title, n.attrs,
               e.rel, e.confidence, e.evidence_chunk_ids, e.edge_id
          FROM cypherx_a1.edges e
          JOIN cypherx_a1.entities n ON {join}
         WHERE e.valid_to IS NULL AND n.valid_to IS NULL {rel_clause}
         ORDER BY e.confidence DESC
         LIMIT %(limit)s
        """,
        params,
    )
    return [dict(r) for r in await cur.fetchall()]


async def owners_of(conn: AsyncConnection, *, entity_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """People connected to ``entity_id`` by an ownership-ish relation, ranked by signal."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT p.entity_id, p.natural_key, p.title, p.attrs,
               array_agg(DISTINCT e.rel) AS rels,
               max(e.confidence) AS confidence,
               count(*) AS signal
          FROM cypherx_a1.edges e
          JOIN cypherx_a1.entities p
            ON p.entity_id = e.src_entity_id AND p.kind = 'person'
         WHERE e.dst_entity_id = %(eid)s
           AND e.rel = ANY(%(rels)s)
           AND e.valid_to IS NULL AND p.valid_to IS NULL
         GROUP BY p.entity_id, p.natural_key, p.title, p.attrs
         ORDER BY confidence DESC, signal DESC
         LIMIT %(limit)s
        """,
        {"eid": entity_id, "rels": list(OWNERSHIP_RELS), "limit": limit},
    )
    return [dict(r) for r in await cur.fetchall()]


async def impact_of(
    conn: AsyncConnection, *, entity_id: str, max_hops: int, limit: int = 50
) -> list[dict[str, Any]]:
    """Reverse-``depends_on`` blast radius: everything that (transitively) depends on the
    target, up to ``max_hops``. A recursive CTE over the current edge slice."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        WITH RECURSIVE blast AS (
            SELECT e.src_entity_id AS entity_id, 1 AS depth
              FROM cypherx_a1.edges e
             WHERE e.dst_entity_id = %(eid)s AND e.rel = 'depends_on' AND e.valid_to IS NULL
            UNION
            SELECT e.src_entity_id, b.depth + 1
              FROM cypherx_a1.edges e
              JOIN blast b ON e.dst_entity_id = b.entity_id
             WHERE e.rel = 'depends_on' AND e.valid_to IS NULL AND b.depth < %(max_hops)s
        )
        SELECT n.entity_id, n.kind, n.natural_key, n.title, n.attrs, min(b.depth) AS depth
          FROM blast b
          JOIN cypherx_a1.entities n ON n.entity_id = b.entity_id AND n.valid_to IS NULL
         GROUP BY n.entity_id, n.kind, n.natural_key, n.title, n.attrs
         ORDER BY depth ASC
         LIMIT %(limit)s
        """,
        {"eid": entity_id, "max_hops": max_hops, "limit": limit},
    )
    return [dict(r) for r in await cur.fetchall()]


async def experts_on(conn: AsyncConnection, *, topic: str, limit: int = 10) -> list[dict[str, Any]]:
    """People with the strongest authored/expert_in/reviewed signal on entities matching
    ``topic`` (FTS). Ranked by aggregate confidence × frequency."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        WITH topic_nodes AS (
            SELECT entity_id FROM cypherx_a1.entities
             WHERE valid_to IS NULL
               AND (fts @@ plainto_tsquery('english', %(topic)s) OR natural_key = %(topic)s)
             LIMIT 200
        )
        SELECT p.entity_id, p.natural_key, p.title, p.attrs,
               count(*) AS signal, sum(e.confidence) AS score,
               array_agg(DISTINCT e.rel) AS rels
          FROM cypherx_a1.edges e
          JOIN topic_nodes tn ON tn.entity_id = e.dst_entity_id
          JOIN cypherx_a1.entities p ON p.entity_id = e.src_entity_id AND p.kind = 'person'
         WHERE e.rel = ANY(%(rels)s) AND e.valid_to IS NULL AND p.valid_to IS NULL
         GROUP BY p.entity_id, p.natural_key, p.title, p.attrs
         ORDER BY score DESC, signal DESC
         LIMIT %(limit)s
        """,
        {"topic": topic, "rels": list(OWNERSHIP_RELS), "limit": limit},
    )
    return [dict(r) for r in await cur.fetchall()]


async def activity_timeline(
    conn: AsyncConnection,
    *,
    scope_entity_id: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Phase B: a time-ordered, cited 'who did what, when' for a scope entity (a repo or a
    person). Returns current change/pr/ticket/incident nodes connected to the scope, each
    with its author and an occurred_at (attrs.timestamp or valid_from), newest first."""
    where = ["acts.occurred_at IS NOT NULL"]
    params: dict[str, Any] = {"scope": scope_entity_id, "limit": limit}
    if since:
        where.append("acts.occurred_at >= %(since)s::timestamptz")
        params["since"] = since
    if until:
        where.append("acts.occurred_at <= %(until)s::timestamptz")
        params["until"] = until
    filter_sql = " AND ".join(where)

    cur = await conn.cursor(row_factory=dict_row).execute(
        f"""
        WITH acts AS (
            SELECT DISTINCT ON (n.entity_id)
                   n.entity_id, n.kind, n.natural_key, n.title, n.attrs, e.rel AS via,
                   COALESCE((n.attrs->>'timestamp')::timestamptz, n.valid_from) AS occurred_at,
                   (SELECT p.title FROM cypherx_a1.edges ea
                      JOIN cypherx_a1.entities p
                        ON p.entity_id = ea.src_entity_id AND p.kind = 'person' AND p.valid_to IS NULL
                     WHERE ea.dst_entity_id = n.entity_id AND ea.rel = 'authored' AND ea.valid_to IS NULL
                     LIMIT 1) AS author,
                   (SELECT p.natural_key FROM cypherx_a1.edges ea
                      JOIN cypherx_a1.entities p
                        ON p.entity_id = ea.src_entity_id AND p.kind = 'person' AND p.valid_to IS NULL
                     WHERE ea.dst_entity_id = n.entity_id AND ea.rel = 'authored' AND ea.valid_to IS NULL
                     LIMIT 1) AS author_key
              FROM cypherx_a1.edges e
              JOIN cypherx_a1.entities n ON (
                    (e.src_entity_id = %(scope)s AND n.entity_id = e.dst_entity_id) OR
                    (e.dst_entity_id = %(scope)s AND n.entity_id = e.src_entity_id))
             WHERE e.valid_to IS NULL AND n.valid_to IS NULL
               AND n.kind IN ('change','pr','ticket','incident')
             ORDER BY n.entity_id, occurred_at DESC
        )
        SELECT * FROM acts
         WHERE {filter_sql}
         ORDER BY occurred_at DESC
         LIMIT %(limit)s
        """,
        params,
    )
    return [dict(r) for r in await cur.fetchall()]


async def consolidation_clusters(
    conn: AsyncConnection, *, rels: tuple[str, ...], min_cluster: int, limit: int = 100
) -> list[dict[str, Any]]:
    """Phase B reflection: per-person clusters of current contribution edges (authored /
    reviewed / owns / expert_in) with their target titles + ids and average confidence —
    the input the consolidator synthesizes into an expertise summary."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT p.entity_id AS person_id, p.natural_key AS person_key, p.title AS person_title,
               count(*) AS cnt, avg(e.confidence)::float8 AS avg_conf,
               (array_agg(DISTINCT t.title))[1:8] AS target_titles,
               (array_agg(t.entity_id))[1:8] AS target_ids
          FROM cypherx_a1.edges e
          JOIN cypherx_a1.entities p
            ON p.entity_id = e.src_entity_id AND p.kind = 'person' AND p.valid_to IS NULL
          JOIN cypherx_a1.entities t ON t.entity_id = e.dst_entity_id AND t.valid_to IS NULL
         WHERE e.valid_to IS NULL AND e.rel = ANY(%(rels)s)
         GROUP BY p.entity_id, p.natural_key, p.title
        HAVING count(*) >= %(min_cluster)s
         LIMIT %(limit)s
        """,
        {"rels": list(rels), "min_cluster": min_cluster, "limit": limit},
    )
    return [dict(r) for r in await cur.fetchall()]


async def keyword_search(
    conn: AsyncConnection, *, query: str, limit: int = 20
) -> list[dict[str, Any]]:
    """FTS leg of hybrid retrieval: current entities ranked by ts_rank, with vector_ref so
    the orchestrator can map a hit back to its RAG chunk for a citation."""
    cur = await conn.cursor(row_factory=dict_row).execute(
        """
        SELECT entity_id, kind, natural_key, title, search_text, vector_ref, created_at,
               COALESCE((SELECT max(ed.confidence) FROM cypherx_a1.edges ed
                          WHERE (ed.src_entity_id = entities.entity_id
                                 OR ed.dst_entity_id = entities.entity_id)
                            AND ed.valid_to IS NULL), 1.0) AS edge_confidence,
               ts_rank(fts, plainto_tsquery('english', %(q)s)) AS rank
          FROM cypherx_a1.entities
         WHERE valid_to IS NULL AND fts @@ plainto_tsquery('english', %(q)s)
         ORDER BY rank DESC
         LIMIT %(limit)s
        """,
        {"q": query, "limit": limit},
    )
    return [dict(r) for r in await cur.fetchall()]
