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

from ..db import outbox
from ..db.pool import in_tenant
from .contradiction import is_contradiction
from .repository import (
    MemoryRepository,
    Session,
    StoredMemory,
    StoreResult,
    WipeResult,
)
from .scoring import ScoringWeights, composite_score

logger = structlog.get_logger(__name__)

_OVERSAMPLE = 4  # candidate window multiplier for the ANN first pass


def _now() -> datetime:
    return datetime.now(UTC)


def _vec_literal(vector: list[float]) -> str:
    """Render a Python float list as a pgvector literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


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
        vector=[],  # not re-read on the hot path
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
    ) -> None:
        self._pool = pool
        self._producer_version = producer_version
        self._default_visibility = default_visibility
        # Contradiction/supersession toggle (default OFF -> today's behavior unchanged).
        self.contradiction_enabled = contradiction_enabled
        self.contradiction_sim_min = contradiction_sim_min

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
    ) -> StoreResult:
        async def _txn(conn: AsyncConnection) -> StoreResult:
            # Dedup: nearest SAME-PRINCIPAL neighbour by cosine distance over the HNSW index.
            # Also fetch the neighbour's content so we can run contradiction detection.
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                SELECT m.id, m.content, (1 - (v.embedding <=> %s::vector)) AS similarity
                  FROM memory.memories m
                  JOIN memory.memory_vectors_1536 v ON v.memory_id = m.id
                 WHERE m.principal_type = %s AND m.principal_id = %s
                   AND m.valid_until IS NULL
                 ORDER BY v.embedding <=> %s::vector
                 LIMIT 1
                """,
                (_vec_literal(memory.vector), memory.principal_type, memory.principal_id,
                 _vec_literal(memory.vector)),
            )
            near = await cur.fetchone()
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
                              superseded_by_id, session_scope_id, agent_scope_id
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
                   importance_score, session_scope_id, agent_scope_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (memory.id, memory.tenant_id, memory.principal_type, memory.principal_id,
                 memory.scope, memory.type, memory.tags, memory.content, Jsonb(memory.metadata),
                 memory.session_id, memory.score, memory.created_at, memory.last_accessed_at,
                 memory.expires_at, memory.importance_score, memory.session_scope_id,
                 memory.agent_scope_id),
            )
            await conn.execute(
                """
                INSERT INTO memory.memory_vectors_1536 (memory_id, tenant_id, embedding)
                VALUES (%s, %s, %s::vector)
                """,
                (memory.id, memory.tenant_id, _vec_literal(memory.vector)),
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
    ) -> list[StoredMemory]:
        qvec = _vec_literal(query_vector)
        # When re-ranking, pull a wider candidate window so the composite can promote a
        # high-importance/recent memory that the pure-ANN order would have dropped.
        oversample = _OVERSAMPLE * (2 if scoring_enabled else 1)
        window = max(top_k * oversample, top_k)

        async def _txn(conn: AsyncConnection) -> list[StoredMemory]:
            cur = await conn.cursor(row_factory=dict_row).execute(
                """
                WITH ann AS (
                    -- PASS 1: ANN over the HNSW index, narrow to a candidate window.
                    SELECT v.memory_id, v.embedding <=> %(qvec)s::vector AS distance
                      FROM memory.memory_vectors_1536 v
                     ORDER BY v.embedding <=> %(qvec)s::vector
                     LIMIT %(window)s
                )
                -- PASS 2: apply visibility + filters on the small candidate set, exact order.
                SELECT m.id, m.tenant_id, m.principal_type, m.principal_id, m.scope, m.type,
                       m.tags, m.content, m.metadata, m.session_id, m.score, m.created_at,
                       m.last_accessed_at, m.expires_at, m.importance_score,
                       m.last_retrieved_at, m.valid_until, m.superseded_by_id,
                       m.session_scope_id, m.agent_scope_id,
                       (1 - ann.distance) AS similarity
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

            if scoring_enabled:
                # Composite re-rank (Generative Agents). Candidate SET unchanged; only order.
                weights = scoring_weights or ScoringWeights()
                now = _now()

                def _key(m: StoredMemory) -> float:
                    ref = m.last_retrieved_at or m.last_accessed_at or m.created_at
                    c = composite_score(
                        cosine=(m.similarity if m.similarity is not None else 0.0),
                        importance=m.importance_score, reference=ref, now=now, weights=weights,
                    )
                    m.composite = c
                    return c

                results = sorted(candidates, key=_key, reverse=True)[:top_k]
            else:
                results = candidates[:top_k]

            # Inline last_accessed_at + last_retrieved_at bump for the returned rows.
            if results:
                ids = [r.id for r in results]
                await conn.execute(
                    "UPDATE memory.memories "
                    "SET last_accessed_at = NOW(), last_retrieved_at = NOW() "
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
                       session_scope_id, agent_scope_id
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
                          session_scope_id, agent_scope_id
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
                       session_scope_id, agent_scope_id
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
