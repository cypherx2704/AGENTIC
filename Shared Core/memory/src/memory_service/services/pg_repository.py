"""PostgreSQL implementation of the memory repository (pgvector HNSW + RLS).

Every tenant-scoped op runs under ``in_tenant`` (sets ``app.tenant_id`` so RLS admits
only this tenant's rows). The runtime role ``mem_user`` is NOT a superuser and does NOT
bypass RLS, so tenant isolation is enforced by the database, not the application.

Cross-PRINCIPAL visibility (within a tenant) is enforced by the SQL predicate, which
mirrors ``scoping.can_view`` exactly:

    owner = caller                                   -- always your own
    OR (scope = 'tenant_shared' AND visibility = 'tenant')  -- shared crosses only here

principal_only memories never cross — that is the cross-end-user leak guard.

Vector search is a TWO-PASS CTE: an ANN pass over the HNSW index narrows to a candidate
window (``top_k * oversample``), then an outer pass applies the visibility predicate +
type/tag filters and orders by exact cosine distance, capped at ``top_k``. The two-pass
shape keeps the index scan fast while the filters run on the small candidate set.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ..core import metrics
from ..db import outbox
from ..db.pool import in_tenant
from .contradiction import is_contradiction
from .linking import LinkCandidate, decide_links
from .repository import (
    MemoryRepository,
    Session,
    StoredMemory,
    StoreResult,
    WipeResult,
)
from .scoring import ScoringWeights, composite_score, mmr_rerank

logger = structlog.get_logger(__name__)

_OVERSAMPLE = 4  # candidate window multiplier for the ANN first pass


def _now() -> datetime:
    return datetime.now(UTC)


def _vec_literal(vector: list[float]) -> str:
    """Render a Python float list as a pgvector literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _parse_vec(raw: Any) -> list[float]:
    """Parse a pgvector value read back from Postgres into a Python float list.

    Registration-independent: pgvector text output is ``[0.1,0.2,...]`` (a str). Also
    tolerates an already-parsed list/tuple. Returns ``[]`` on anything unexpected so a
    missing/embedding-less row degrades to relevance-only MMR instead of raising.
    """
    if raw is None:
        return []
    if isinstance(raw, list | tuple):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return []
    try:
        return [float(x) for x in s.split(",")]
    except ValueError:
        return []


# ── B1: which cast the ANN scan uses. The base vector(1536) column is unchanged; these
# just pick the index/cast so the planner scans the smaller quantized index when asked. ──
_DIM = 1536


def _ann_distance_expr(qparam: str, quantization: str) -> str:
    """The TRUE-precision-ish cosine distance surfaced for a candidate (and its similarity).

    halfvec: the halfvec cosine distance (near-identical to full cosine). off/binary_rerank:
    the full-precision vector cosine distance (binary_rerank uses the coarse bit index only
    for candidate SELECTION, then reranks on this exact cosine).
    """
    if quantization == "halfvec":
        return f"v.embedding::halfvec({_DIM}) <=> {qparam}::halfvec({_DIM})"
    return f"v.embedding <=> {qparam}::vector"


def _ann_order_expr(qparam: str, quantization: str) -> str:
    """The ORDER BY expression the ANN first pass uses to pick the candidate window.

    Chosen so the planner scans the matching HNSW index: the halfvec expression index for
    'halfvec', the bit Hamming expression index for 'binary_rerank', the full-precision index
    for 'off'.
    """
    if quantization == "halfvec":
        return f"v.embedding::halfvec({_DIM}) <=> {qparam}::halfvec({_DIM})"
    if quantization == "binary_rerank":
        return (
            f"binary_quantize(v.embedding)::bit({_DIM}) "
            f"<~> binary_quantize({qparam}::vector)::bit({_DIM})"
        )
    return f"v.embedding <=> {qparam}::vector"


def _dedup_order_expr(qparam: str, quantization: str) -> str:
    """ORDER BY for the store() dedup nearest-neighbour scan.

    Uses the halfvec expression index when any quantization is on (halfvec is precise enough
    for the >=0.95 dedup decision; the coarse bit index is NOT used for dedup). The dedup
    similarity VALUE is always computed at full precision regardless.
    """
    if quantization in ("halfvec", "binary_rerank"):
        return f"v.embedding::halfvec({_DIM}) <=> {qparam}::halfvec({_DIM})"
    return f"v.embedding <=> {qparam}::vector"


def _row_to_memory(row: dict[str, Any]) -> StoredMemory:
    return StoredMemory(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        principal_type=row["principal_type"],
        principal_id=str(row["principal_id"]),
        scope=row["scope"],
        type=row["type"],
        tags=list(row["tags"] or []),
        content=row["content"],
        metadata=dict(row["metadata"] or {}),
        # Vector is not re-read on the hot path; B6 MMR fetches v.embedding conditionally and
        # populates it (else it stays [], keeping the default path byte-identical).
        vector=_parse_vec(row.get("embedding")),
        session_id=str(row["session_id"]) if row.get("session_id") else None,
        score=float(row["score"]),
        created_at=row["created_at"],
        last_accessed_at=row["last_accessed_at"],
        expires_at=row.get("expires_at"),
        # ── Additive columns (migration #2). .get keeps this tolerant of older rows. ──
        importance_score=float(row["importance_score"]) if row.get("importance_score") is not None else 0.5,
        last_retrieved_at=row.get("last_retrieved_at"),
        valid_until=row.get("valid_until"),
        superseded_by_id=str(row["superseded_by_id"]) if row.get("superseded_by_id") else None,
        session_scope_id=row.get("session_scope_id"),
        agent_scope_id=row.get("agent_scope_id"),
        access_count=int(row["access_count"]) if row.get("access_count") is not None else 0,
        similarity=float(row["similarity"]) if row.get("similarity") is not None else None,
    )


class PgMemoryRepository(MemoryRepository):
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        producer_version: str,
        default_visibility: str,
        contradiction_enabled: bool = False,
        contradiction_sim_min: float = 0.80,
        vector_quantization: str = "off",
        hnsw_ef_search: int = 0,
    ) -> None:
        self._pool = pool
        self._producer_version = producer_version
        self._default_visibility = default_visibility
        # Contradiction/supersession toggle (default OFF -> today's behavior unchanged).
        self.contradiction_enabled = contradiction_enabled
        self.contradiction_sim_min = contradiction_sim_min
        # ── B1: which vector type the ANN scan casts to (off|halfvec|binary_rerank) ──────
        self.vector_quantization = vector_quantization
        # ── B3: per-query hnsw.ef_search GUC; 0 => do not emit (preserve pgvector default) ─
        self.hnsw_ef_search = hnsw_ef_search

    # ── tenant config ────────────────────────────────────────────────────────────
    async def get_tenant_visibility(self, tenant_id: str) -> str:
        async def _fn(conn: AsyncConnection) -> str:
            cur = await conn.execute(
                "SELECT user_scope_visibility FROM memory.tenant_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = await cur.fetchone()
            return row[0] if row else self._default_visibility

        return await in_tenant(self._pool, tenant_id, _fn)

    async def get_tenant_dedup_threshold(self, tenant_id: str, default: float) -> float:
        async def _fn(conn: AsyncConnection) -> float:
            cur = await conn.execute(
                "SELECT dedup_threshold FROM memory.tenant_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = await cur.fetchone()
            return float(row[0]) if row and row[0] is not None else default

        return await in_tenant(self._pool, tenant_id, _fn)

    async def resource_usage(
        self, tenant_id: str, principal_type: str, principal_id: str
    ) -> tuple[int, int]:
        async def _fn(conn: AsyncConnection) -> tuple[int, int]:
            cur = await conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(octet_length(content)), 0)
                  FROM memory.memories
                 WHERE principal_type = %s AND principal_id = %s
                """,
                (principal_type, principal_id),
            )
            row = await cur.fetchone()
            return (int(row[0]), int(row[1])) if row else (0, 0)

        return await in_tenant(self._pool, tenant_id, _fn)

    # ── store (dedup-bump in one txn) ──────────────────────────────────────────────
    async def store(
        self,
        *,
        memory: StoredMemory,
        dedup_threshold: float,
        trace_id: str,
        producer_version: str,
        linking_enabled: bool = False,
        linking_sim_min: float = 0.50,
        linking_max_neighbors: int = 3,
    ) -> StoreResult:
        qvec = _vec_literal(memory.vector)
        dedup_order = _dedup_order_expr("%(qvec)s", self.vector_quantization)
        # When linking, fetch a small neighbour window (nearest is the dedup candidate; the
        # rest feed the link decisions) — one query, reused. Else just the single nearest.
        neighbour_limit = max(linking_max_neighbors + 1, 1) if linking_enabled else 1

        async def _txn(conn: AsyncConnection) -> StoreResult:
            await self._maybe_set_ef_search(conn)  # B3: query-time HNSW candidate list
            # Dedup: nearest SAME-PRINCIPAL neighbour by cosine distance over the HNSW index.
            # Similarity is ALWAYS full precision; only the index scan may use a quantized cast.
            cur = await conn.cursor(row_factory=dict_row).execute(
                f"""
                SELECT m.id, m.content, (1 - (v.embedding <=> %(qvec)s::vector)) AS similarity
                  FROM memory.memories m
                  JOIN memory.memory_vectors_1536 v ON v.memory_id = m.id
                 WHERE m.principal_type = %(ptype)s AND m.principal_id = %(pid)s
                   AND m.valid_until IS NULL
                 ORDER BY {dedup_order}
                 LIMIT %(lim)s
                """,
                {"qvec": qvec, "ptype": memory.principal_type, "pid": memory.principal_id,
                 "lim": neighbour_limit},
            )
            neighbours = await cur.fetchall()
            near = neighbours[0] if neighbours else None
            if near is not None and float(near["similarity"]) >= dedup_threshold:
                # BUMP-ONLY: do not insert a near-duplicate; bump the existing row.
                cur = await conn.cursor(row_factory=dict_row).execute(
                    """
                    UPDATE memory.memories
                       SET last_accessed_at = NOW(), score = score + 1
                     WHERE id = %s
                    RETURNING id, tenant_id, principal_type, principal_id, scope, type, tags,
                              content, metadata, session_id, score, created_at, last_accessed_at,
                              expires_at, importance_score, last_retrieved_at, valid_until,
                              superseded_by_id, session_scope_id, agent_scope_id, access_count
                    """,
                    (near["id"],),
                )
                bumped = await cur.fetchone()
                await outbox.emit(
                    conn, topic=outbox.TOPIC_MEMORY_STORED, tenant_id=memory.tenant_id,
                    trace_id=trace_id,
                    payload={"memory_id": str(near["id"]), "deduped": True,
                             "principal_type": memory.principal_type,
                             "principal_id": memory.principal_id},
                    producer_version=producer_version,
                )
                return StoreResult(memory=_row_to_memory(bumped), deduped=True)

            # ── Contradiction / temporal validity (flag-guarded; OFF -> skipped) ──────
            # The nearest valid neighbour conflicts (same subject, asserted value, not an
            # exact dup) -> mark it SUPERSEDED by the new memory (keep the row for audit).
            if (
                self.contradiction_enabled
                and near is not None
                and is_contradiction(
                    new_content=memory.content,
                    prior_content=near.get("content") or "",
                    cosine_similarity=float(near["similarity"]),
                    sim_min=self.contradiction_sim_min,
                    dedup_threshold=dedup_threshold,
                )
            ):
                await conn.execute(
                    """
                    UPDATE memory.memories
                       SET valid_until = NOW(), superseded_by_id = %s
                     WHERE id = %s
                    """,
                    (memory.id, near["id"]),
                )
                await conn.execute(
                    """
                    INSERT INTO memory.memory_audit
                      (tenant_id, memory_id, principal_type, principal_id, action,
                       reason, summary_memory_id)
                    VALUES (%s,%s,%s,%s,'superseded',%s,%s)
                    """,
                    (memory.tenant_id, near["id"], memory.principal_type, memory.principal_id,
                     "superseded by newer conflicting memory", memory.id),
                )

            # INSERT the new memory + its vector + the stored event, all in this txn.
            await conn.execute(
                """
                INSERT INTO memory.memories
                  (id, tenant_id, principal_type, principal_id, scope, type, tags, content,
                   metadata, session_id, score, created_at, last_accessed_at, expires_at,
                   importance_score, session_scope_id, agent_scope_id, access_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (memory.id, memory.tenant_id, memory.principal_type, memory.principal_id,
                 memory.scope, memory.type, memory.tags, memory.content, Jsonb(memory.metadata),
                 memory.session_id, memory.score, memory.created_at, memory.last_accessed_at,
                 memory.expires_at, memory.importance_score, memory.session_scope_id,
                 memory.agent_scope_id, memory.access_count),
            )
            await conn.execute(
                """
                INSERT INTO memory.memory_vectors_1536 (memory_id, tenant_id, embedding)
                VALUES (%s, %s, %s::vector)
                """,
                (memory.id, memory.tenant_id, _vec_literal(memory.vector)),
            )
            # ── B7: write associative edges to related neighbours (flag-guarded, in-txn) ──
            if linking_enabled and neighbours:
                await self._write_links(
                    conn, memory=memory, neighbours=neighbours,
                    dedup_threshold=dedup_threshold, sim_min=linking_sim_min,
                    max_neighbors=linking_max_neighbors,
                )
            await outbox.emit(
                conn, topic=outbox.TOPIC_MEMORY_STORED, tenant_id=memory.tenant_id,
                trace_id=trace_id,
                payload={"memory_id": memory.id, "deduped": False,
                         "principal_type": memory.principal_type, "principal_id": memory.principal_id,
                         "bytes": len(memory.content.encode("utf-8"))},
                producer_version=producer_version,
            )
            return StoreResult(memory=memory, deduped=False)

        return await in_tenant(self._pool, memory.tenant_id, _txn)

    # ── B3: query-time HNSW ef_search GUC (transaction-local; only when configured) ──────
    async def _maybe_set_ef_search(self, conn: AsyncConnection) -> None:
        """Emit ``SET LOCAL hnsw.ef_search = <n>`` when configured (>0), else no-op.

        Bounds how many candidates the HNSW scan returns regardless of SQL LIMIT (pgvector
        default 40) — raising it toward the oversample window makes the two-pass oversample
        real. Runs inside the tenant transaction (in_tenant), so it is transaction-local. The
        value is a validated int from config, interpolated as a literal because Postgres SET
        does not accept bind params.
        """
        n = int(self.hnsw_ef_search)
        if n > 0:
            await conn.execute(f"SET LOCAL hnsw.ef_search = {n}")

    # ── B7: write associative edges from a freshly-inserted memory to its neighbours ─────
    async def _write_links(
        self,
        conn: AsyncConnection,
        *,
        memory: StoredMemory,
        neighbours: list[dict[str, Any]],
        dedup_threshold: float,
        sim_min: float,
        max_neighbors: int,
    ) -> None:
        """Insert directed edges (both directions) to the associated neighbours, in-txn.

        Uses the SAME neighbour window the dedup scan fetched (no extra query). Fails soft:
        an ON CONFLICT DO NOTHING keeps a concurrent/duplicate edge from erroring the store.
        """
        decisions = decide_links(
            [
                LinkCandidate(memory_id=str(n["id"]), similarity=float(n["similarity"]))
                for n in neighbours
            ],
            sim_min=sim_min, dedup_threshold=dedup_threshold, max_neighbors=max_neighbors,
        )
        written = 0
        for d in decisions:
            for src, dst in ((memory.id, d.dst_memory_id), (d.dst_memory_id, memory.id)):
                await conn.execute(
                    """
                    INSERT INTO memory.memory_links
                      (tenant_id, principal_type, principal_id, src_memory_id, dst_memory_id,
                       relation, weight)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id, src_memory_id, dst_memory_id) DO NOTHING
                    """,
                    (memory.tenant_id, memory.principal_type, memory.principal_id, src, dst,
                     d.relation, d.weight),
                )
            written += 1
        if written:
            metrics.links_written_total.inc(written)

    # ── B7: bounded 1-hop link expansion at retrieval (embedding-free; one round-trip) ───
    async def _expand_links(
        self,
        conn: AsyncConnection,
        *,
        seed_ids: list[str],
        exclude_ids: list[str],
        qvec: str,
        caller_type: str,
        caller_id: str,
        user_scope_visibility: str,
        include_shared: bool,
        current_only: bool,
        type_filter: str | None,
        tags_filter: list[str] | None,
        session_scope_id: str | None,
        agent_scope_id: str | None,
        want_embedding: bool,
        limit: int,
    ) -> list[StoredMemory]:
        """Fetch memories 1 hop from ``seed_ids`` over memory_links, minus ``exclude_ids``.

        Re-applies the EXACT search visibility + filter predicate so the expansion can never
        surface a memory the caller can't see. Computes similarity against the already-known
        ``qvec`` (no new embed call). No new HNSW scan; a plain index join.
        """
        emb_sel = ", v.embedding::text AS embedding" if want_embedding else ""
        cur = await conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT DISTINCT m.id, m.tenant_id, m.principal_type, m.principal_id, m.scope, m.type,
                   m.tags, m.content, m.metadata, m.session_id, m.score, m.created_at,
                   m.last_accessed_at, m.expires_at, m.importance_score,
                   m.last_retrieved_at, m.valid_until, m.superseded_by_id,
                   m.session_scope_id, m.agent_scope_id, m.access_count,
                   (1 - (v.embedding <=> %(qvec)s::vector)) AS similarity{emb_sel}
              FROM memory.memory_links l
              JOIN memory.memories m ON m.id = l.dst_memory_id
              JOIN memory.memory_vectors_1536 v ON v.memory_id = m.id
             WHERE l.src_memory_id = ANY(%(seed_ids)s::uuid[])
               AND m.id <> ALL(%(exclude_ids)s::uuid[])
               AND (m.expires_at IS NULL OR m.expires_at > NOW())
               AND (NOT %(current_only)s::boolean OR m.valid_until IS NULL)
               AND (
                     (m.principal_type = %(ctype)s::text AND m.principal_id = %(cid)s::text)
                  OR (%(include_shared)s::boolean AND m.scope = 'tenant_shared'
                      AND %(visibility)s::text = 'tenant')
                   )
               AND (%(type)s::text IS NULL OR m.type = %(type)s::text)
               AND (%(tags)s::text[] IS NULL OR m.tags @> %(tags)s::text[])
               AND (%(session_scope)s::text IS NULL
                    OR m.session_scope_id = %(session_scope)s::text)
               AND (%(agent_scope)s::text IS NULL
                    OR m.agent_scope_id = %(agent_scope)s::text)
             LIMIT %(limit)s
            """,
            {
                "qvec": qvec, "seed_ids": seed_ids, "exclude_ids": exclude_ids,
                "ctype": caller_type, "cid": caller_id, "include_shared": include_shared,
                "visibility": user_scope_visibility, "type": type_filter, "tags": tags_filter,
                "current_only": current_only, "session_scope": session_scope_id,
                "agent_scope": agent_scope_id, "limit": limit,
            },
        )
        rows = await cur.fetchall()
        return [_row_to_memory(r) for r in rows]

    # ── search (two-pass CTE) ──────────────────────────────────────────────────────
    async def search(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        query_vector: list[float],
        top_k: int,
        type_filter: str | None,
        tags_filter: list[str] | None,
        include_shared: bool,
        user_scope_visibility: str,
        scoring_enabled: bool = False,
        scoring_weights: ScoringWeights | None = None,
        current_only: bool = False,
        session_scope_id: str | None = None,
        agent_scope_id: str | None = None,
        mmr_enabled: bool = False,
        mmr_lambda: float = 0.5,
        linking_enabled: bool = False,
        link_expansion_limit: int = 10,
    ) -> list[StoredMemory]:
        qvec = _vec_literal(query_vector)
        # When re-ranking (composite or MMR), pull a wider candidate window so the re-rank can
        # promote a memory the pure-ANN order would have dropped / find diverse facets.
        oversample = _OVERSAMPLE * (2 if (scoring_enabled or mmr_enabled) else 1)
        window = max(top_k * oversample, top_k)
        # B1: cast the ANN scan to the selected quantized type (planner picks the index).
        qdist = _ann_distance_expr("%(qvec)s", self.vector_quantization)
        qorder = _ann_order_expr("%(qvec)s", self.vector_quantization)
        # B6: only fetch the candidate vectors when MMR needs them (else byte-identical).
        emb_cte = ", v.embedding::text AS embedding" if mmr_enabled else ""
        emb_outer = ", ann.embedding AS embedding" if mmr_enabled else ""

        async def _txn(conn: AsyncConnection) -> list[StoredMemory]:
            await self._maybe_set_ef_search(conn)  # B3: query-time HNSW candidate list
            cur = await conn.cursor(row_factory=dict_row).execute(
                f"""
                WITH ann AS (
                    -- PASS 1: ANN over the HNSW index, narrow to a candidate window.
                    SELECT v.memory_id, {qdist} AS distance{emb_cte}
                      FROM memory.memory_vectors_1536 v
                     ORDER BY {qorder}
                     LIMIT %(window)s
                )
                -- PASS 2: apply visibility + filters on the small candidate set, exact order.
                SELECT m.id, m.tenant_id, m.principal_type, m.principal_id, m.scope, m.type,
                       m.tags, m.content, m.metadata, m.session_id, m.score, m.created_at,
                       m.last_accessed_at, m.expires_at, m.importance_score,
                       m.last_retrieved_at, m.valid_until, m.superseded_by_id,
                       m.session_scope_id, m.agent_scope_id, m.access_count,
                       (1 - ann.distance) AS similarity{emb_outer}
                  FROM ann
                  JOIN memory.memories m ON m.id = ann.memory_id
                 WHERE (m.expires_at IS NULL OR m.expires_at > NOW())
                   AND (NOT %(current_only)s::boolean OR m.valid_until IS NULL)
                   AND (
                         (m.principal_type = %(ctype)s::text AND m.principal_id = %(cid)s::text)
                      OR (%(include_shared)s::boolean AND m.scope = 'tenant_shared'
                          AND %(visibility)s::text = 'tenant')
                       )
                   AND (%(type)s::text IS NULL OR m.type = %(type)s::text)
                   AND (%(tags)s::text[] IS NULL OR m.tags @> %(tags)s::text[])
                   AND (%(session_scope)s::text IS NULL
                        OR m.session_scope_id = %(session_scope)s::text)
                   AND (%(agent_scope)s::text IS NULL
                        OR m.agent_scope_id = %(agent_scope)s::text)
                 ORDER BY ann.distance
                 LIMIT %(window)s
                """,
                {
                    "qvec": qvec,
                    "window": window,
                    "ctype": caller_type,
                    "cid": caller_id,
                    "include_shared": include_shared,
                    "visibility": user_scope_visibility,
                    "type": type_filter,
                    "tags": tags_filter,
                    "current_only": current_only,
                    "session_scope": session_scope_id,
                    "agent_scope": agent_scope_id,
                },
            )
            rows = await cur.fetchall()
            candidates = [_row_to_memory(r) for r in rows]

            now = _now()
            if mmr_enabled:
                # B6: diversity re-rank of the candidate window. Fail-soft to ANN order.
                try:
                    results = mmr_rerank(
                        candidates, query_vector, lambda_mult=mmr_lambda, top_k=top_k
                    )
                    metrics.mmr_reranked_total.inc()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mmr_rerank_failed", error=str(exc))
                    results = candidates[:top_k]
            elif scoring_enabled:
                # Composite re-rank (Generative Agents / ACT-R). Set unchanged; only order.
                weights = scoring_weights or ScoringWeights()

                def _key(m: StoredMemory) -> float:
                    ref = m.last_retrieved_at or m.last_accessed_at or m.created_at
                    c = composite_score(
                        cosine=(m.similarity if m.similarity is not None else 0.0),
                        importance=m.importance_score, reference=ref, now=now, weights=weights,
                        access_count=m.access_count,
                    )
                    m.composite = c
                    return c

                results = sorted(candidates, key=_key, reverse=True)[:top_k]
            else:
                results = candidates[:top_k]

            # ── B7: bounded 1-hop, embedding-free link expansion (one extra round-trip) ──
            # Walk edges from the ranked top_k and APPEND visible linked memories the ANN
            # missed (relevant by association, far from the query vector). No new embed/vector
            # call. Fail-soft: on any error keep the vector-only set.
            if linking_enabled and results:
                try:
                    have = {r.id for r in results}
                    expanded = await self._expand_links(
                        conn, seed_ids=[r.id for r in results], exclude_ids=list(have), qvec=qvec,
                        caller_type=caller_type, caller_id=caller_id,
                        user_scope_visibility=user_scope_visibility, include_shared=include_shared,
                        current_only=current_only, type_filter=type_filter,
                        tags_filter=tags_filter, session_scope_id=session_scope_id,
                        agent_scope_id=agent_scope_id, want_embedding=mmr_enabled,
                        limit=link_expansion_limit,
                    )
                    fresh = [m for m in expanded if m.id not in have]
                    if fresh:
                        results = results + fresh
                        metrics.link_expanded_total.inc(len(fresh))
                except Exception as exc:  # noqa: BLE001 — expansion is additive: fail soft
                    logger.warning("link_expansion_failed", error=str(exc))

            # Inline last_accessed_at + last_retrieved_at + access_count bump for returned rows.
            if results:
                ids = [r.id for r in results]
                await conn.execute(
                    "UPDATE memory.memories "
                    "SET last_accessed_at = NOW(), last_retrieved_at = NOW(), "
                    "access_count = access_count + 1 "
                    "WHERE id = ANY(%s::uuid[])",
                    (ids,),
                )
            return results

        return await in_tenant(self._pool, tenant_id, _txn)

    # ── by-id ───────────────────────────────────────────────────────────────────────
    async def get_by_id(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        user_scope_visibility: str,
    ) -> StoredMemory | None:
        async def _txn(conn: AsyncConnection) -> StoredMemory | None:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT id, tenant_id, principal_type, principal_id, scope, type, tags, content,
                       metadata, session_id, score, created_at, last_accessed_at, expires_at,
                       importance_score, last_retrieved_at, valid_until, superseded_by_id,
                       session_scope_id, agent_scope_id, access_count
                  FROM memory.memories
                 WHERE id = %(id)s
                   AND (
                         (principal_type = %(ctype)s::text AND principal_id = %(cid)s::text)
                      OR (scope = 'tenant_shared' AND %(visibility)s::text = 'tenant')
                       )
                """,
                {"id": memory_id, "ctype": caller_type, "cid": caller_id,
                 "visibility": user_scope_visibility},
            )
            row = await cur.fetchone()
            return _row_to_memory(row) if row else None

        try:
            return await in_tenant(self._pool, tenant_id, _txn)
        except Exception as exc:  # noqa: BLE001 — a malformed UUID etc. is a not-found
            logger.info("get_by_id_miss", error=str(exc))
            return None

    async def update(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        changes: dict[str, Any],
    ) -> StoredMemory | None:
        async def _txn(conn: AsyncConnection) -> StoredMemory | None:
            sets: list[str] = []
            params: dict[str, Any] = {"id": memory_id, "ctype": caller_type, "cid": caller_id}
            if changes.get("content") is not None:
                sets.append("content = %(content)s")
                params["content"] = changes["content"]
            if changes.get("scope") is not None:
                sets.append("scope = %(scope)s")
                params["scope"] = changes["scope"]
            if changes.get("tags") is not None:
                sets.append("tags = %(tags)s")
                params["tags"] = changes["tags"]
            if changes.get("metadata") is not None:
                sets.append("metadata = %(metadata)s")
                params["metadata"] = Jsonb(changes["metadata"])
            if "expires_at" in changes:
                sets.append("expires_at = %(expires_at)s")
                params["expires_at"] = changes["expires_at"]
            sets.append("last_accessed_at = NOW()")

            cur = await conn.cursor(row_factory=dict_row).execute(
                f"""
                UPDATE memory.memories
                   SET {", ".join(sets)}
                 WHERE id = %(id)s AND principal_type = %(ctype)s AND principal_id = %(cid)s
                RETURNING id, tenant_id, principal_type, principal_id, scope, type, tags, content,
                          metadata, session_id, score, created_at, last_accessed_at, expires_at,
                          importance_score, last_retrieved_at, valid_until, superseded_by_id,
                          session_scope_id, agent_scope_id, access_count
                """,
                params,
            )
            row = await cur.fetchone()
            return _row_to_memory(row) if row else None

        try:
            return await in_tenant(self._pool, tenant_id, _txn)
        except Exception as exc:  # noqa: BLE001
            logger.info("update_miss", error=str(exc))
            return None

    async def delete(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        trace_id: str,
        producer_version: str,
    ) -> bool:
        async def _txn(conn: AsyncConnection) -> bool:
            cur = await conn.execute(
                """
                DELETE FROM memory.memories
                 WHERE id = %s AND principal_type = %s AND principal_id = %s
                RETURNING id
                """,
                (memory_id, caller_type, caller_id),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            await outbox.emit(
                conn, topic=outbox.TOPIC_MEMORY_DELETED, tenant_id=tenant_id, trace_id=trace_id,
                payload={"memory_id": memory_id, "principal_type": caller_type,
                         "principal_id": caller_id},
                producer_version=producer_version,
            )
            return True

        try:
            return await in_tenant(self._pool, tenant_id, _txn)
        except Exception as exc:  # noqa: BLE001
            logger.info("delete_miss", error=str(exc))
            return False

    # ── sessions (idempotent create; 409 on cross-principal collision) ─────────────
    async def create_session(self, *, session: Session) -> tuple[Session, bool]:
        async def _txn(conn: AsyncConnection) -> tuple[Session, bool]:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT session_id, tenant_id, principal_type, principal_id, title, metadata,
                       created_at
                  FROM memory.sessions
                 WHERE session_id = %s
                """,
                (session.session_id,),
            )
            existing = await cur.fetchone()
            if existing is not None:
                same_principal = (
                    existing["principal_type"] == session.principal_type
                    and str(existing["principal_id"]) == session.principal_id
                )
                return (
                    Session(
                        session_id=existing["session_id"],
                        tenant_id=str(existing["tenant_id"]),
                        principal_type=existing["principal_type"],
                        principal_id=str(existing["principal_id"]),
                        title=existing["title"],
                        metadata=dict(existing["metadata"] or {}),
                        created_at=existing["created_at"],
                    ),
                    same_principal,
                )
            try:
                # Nested savepoint: a UNIQUE violation here aborts ONLY this INSERT and leaves the
                # outer tenant transaction committable (psycopg poisons the whole tx otherwise).
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO memory.sessions
                          (session_id, tenant_id, principal_type, principal_id, title, metadata, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (session.session_id, session.tenant_id, session.principal_type,
                         session.principal_id, session.title, Jsonb(session.metadata), session.created_at),
                    )
            except UniqueViolation:
                # session_id is a GLOBAL primary key, but RLS hid an owner row in ANOTHER tenant from
                # the SELECT above (or a same-tenant concurrent create won the race). Map to the
                # existing 409 SESSION_PRINCIPAL_COLLISION path instead of an unhandled 500.
                logger.info("session_create_collision", session_id=session.session_id)
                return session, False
            return session, True

        return await in_tenant(self._pool, session.tenant_id, _txn)

    # ── GDPR bulk wipe — log + delete + event in ONE transaction ───────────────────
    async def gdpr_wipe(
        self,
        *,
        tenant_id: str,
        principal_type: str,
        principal_id: str,
        requested_by: str,
        reason: str | None,
        trace_id: str,
        producer_version: str,
    ) -> WipeResult:
        wipe_log_id = str(uuid.uuid4())

        async def _txn(conn: AsyncConnection) -> WipeResult:
            cur = await conn.execute(
                """
                DELETE FROM memory.memories
                 WHERE principal_type = %s AND principal_id = %s
                """,
                (principal_type, principal_id),
            )
            deleted_count = cur.rowcount or 0
            await conn.execute(
                "DELETE FROM memory.sessions WHERE principal_type = %s AND principal_id = %s",
                (principal_type, principal_id),
            )
            await conn.execute(
                """
                INSERT INTO memory.gdpr_wipe_log
                  (id, tenant_id, principal_type, principal_id, deleted_count, reason,
                   requested_by, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                """,
                (wipe_log_id, tenant_id, principal_type, principal_id, deleted_count, reason,
                 requested_by),
            )
            await outbox.emit(
                conn, topic=outbox.TOPIC_GDPR_WIPED, tenant_id=tenant_id, trace_id=trace_id,
                payload={"principal_type": principal_type, "principal_id": principal_id,
                         "deleted_count": deleted_count, "wipe_log_id": wipe_log_id,
                         "reason": reason},
                producer_version=producer_version,
            )
            return WipeResult(deleted_count=deleted_count, wipe_log_id=wipe_log_id)

        return await in_tenant(self._pool, tenant_id, _txn)

    # ── TTL sweep (cross-tenant batch; runs WITHOUT app.tenant_id, like the publisher)
    async def sweep_expired(self, *, batch_size: int) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                DELETE FROM memory.memories
                 WHERE id IN (
                    SELECT id FROM memory.memories
                     WHERE expires_at IS NOT NULL AND expires_at <= NOW()
                     LIMIT %s
                 )
                """,
                (batch_size,),
            )
            return cur.rowcount or 0

    # ── Consolidation / forgetting (opt-in routine; cross-tenant batch read) ───────
    async def consolidation_candidates(
        self, *, max_importance: float, min_age_seconds: float, batch_size: int
    ) -> list[StoredMemory]:
        async with self._pool.connection() as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT id, tenant_id, principal_type, principal_id, scope, type, tags, content,
                       metadata, session_id, score, created_at, last_accessed_at, expires_at,
                       importance_score, last_retrieved_at, valid_until, superseded_by_id,
                       session_scope_id, agent_scope_id, access_count
                  FROM memory.memories
                 WHERE valid_until IS NULL
                   AND importance_score <= %s
                   AND COALESCE(last_retrieved_at, last_accessed_at, created_at)
                       <= NOW() - make_interval(secs => %s)
                 ORDER BY COALESCE(last_retrieved_at, last_accessed_at, created_at) ASC
                 LIMIT %s
                """,
                (max_importance, float(min_age_seconds), batch_size),
            )
            rows = await cur.fetchall()
            return [_row_to_memory(r) for r in rows]

    async def soft_delete_to_audit(
        self, *, memory: StoredMemory, action: str, reason: str | None,
        summary_memory_id: str | None,
    ) -> bool:
        async def _txn(conn: AsyncConnection) -> bool:
            snapshot = {
                "content": memory.content, "type": memory.type, "tags": memory.tags,
                "scope": memory.scope, "importance_score": memory.importance_score,
                "created_at": memory.created_at.isoformat() if memory.created_at else None,
            }
            await conn.execute(
                """
                INSERT INTO memory.memory_audit
                  (tenant_id, memory_id, principal_type, principal_id, action, reason,
                   summary_memory_id, snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (memory.tenant_id, memory.id, memory.principal_type, memory.principal_id,
                 action, reason, summary_memory_id, Jsonb(snapshot)),
            )
            cur = await conn.execute(
                "DELETE FROM memory.memories WHERE id = %s RETURNING id", (memory.id,)
            )
            return await cur.fetchone() is not None

        return await in_tenant(self._pool, memory.tenant_id, _txn)
