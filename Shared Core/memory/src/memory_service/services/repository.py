"""Memory persistence — an abstract repository with a Postgres + an in-memory impl.

Both implementations share the SAME ownership/visibility rules (``scoping.can_view``) and
the SAME dedup semantics, so the in-memory repo (used by the deterministic test suite and
the ``db_pool=None`` degradation path) behaves identically to the Postgres repo (used in
production with pgvector HNSW + RLS).

The repository is the ONLY place that touches stored memories. Each tenant-scoped op runs
under the tenant transaction (``in_tenant`` sets ``app.tenant_id`` for RLS) for the
Postgres impl. Atomicity guarantees (store+outbox, GDPR log+delete+event in one txn) are
provided by the repo, not the API layer.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import scoping
from .scoring import ScoringWeights, composite_score, heuristic_importance, mmr_rerank
from .similarity import cosine_similarity


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class StoredMemory:
    """One persisted memory (repo-internal; the API maps it to a wire MemoryRecord)."""

    id: str
    tenant_id: str
    principal_type: str
    principal_id: str
    scope: str
    type: str
    tags: list[str]
    content: str
    metadata: dict[str, Any]
    vector: list[float]
    session_id: str | None
    score: float
    created_at: datetime
    last_accessed_at: datetime
    expires_at: datetime | None
    # ── Additive scoring / validity / richer-scope columns (migration #2) ──────────
    # All have today's-behavior-preserving defaults so the pure-cosine path is unchanged.
    importance_score: float = 0.5
    last_retrieved_at: datetime | None = None
    valid_until: datetime | None = None  # NULL = currently valid
    superseded_by_id: str | None = None
    session_scope_id: str | None = None
    agent_scope_id: str | None = None
    # ── B4: retrieval-frequency counter for ACT-R base-level activation (migration #6) ──
    # How many times this memory has been RETURNED by a search. Only read under
    # MEMORY_SCORING_DECAY='power_actr'; default 0 leaves the exponential path unchanged.
    access_count: int = 0
    # Transient (not persisted): set on search results so the API can surface it.
    similarity: float | None = None
    # Transient: the composite re-rank score (only set when MEMORY_SCORING_ENABLED).
    composite: float | None = None


@dataclass
class StoreResult:
    memory: StoredMemory
    deduped: bool


@dataclass
class WipeResult:
    deleted_count: int
    wipe_log_id: str


@dataclass
class Session:
    session_id: str
    tenant_id: str
    principal_type: str
    principal_id: str
    title: str | None
    metadata: dict[str, Any]
    created_at: datetime


class MemoryRepository(ABC):
    """The persistence seam shared by the Postgres + in-memory implementations."""

    @abstractmethod
    async def get_tenant_visibility(self, tenant_id: str) -> str: ...

    @abstractmethod
    async def get_tenant_dedup_threshold(self, tenant_id: str, default: float) -> float: ...

    @abstractmethod
    async def resource_usage(
        self, tenant_id: str, principal_type: str, principal_id: str
    ) -> tuple[int, int]: ...

    @abstractmethod
    async def store(
        self,
        *,
        memory: StoredMemory,
        dedup_threshold: float,
        trace_id: str,
        producer_version: str,
        # ── B7 associative linking (additive; default off => no edges written) ──────
        linking_enabled: bool = False,
        linking_sim_min: float = 0.50,
        linking_max_neighbors: int = 3,
    ) -> StoreResult: ...

    @abstractmethod
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
        # ── Additive (defaults reproduce today's pure-cosine, all-rows behavior) ──────
        scoring_enabled: bool = False,
        scoring_weights: ScoringWeights | None = None,
        current_only: bool = False,
        session_scope_id: str | None = None,
        agent_scope_id: str | None = None,
        # ── B6 MMR diversity re-rank + B7 link expansion (default off => unchanged) ──
        mmr_enabled: bool = False,
        mmr_lambda: float = 0.5,
        linking_enabled: bool = False,
        link_expansion_limit: int = 10,
    ) -> list[StoredMemory]: ...

    @abstractmethod
    async def get_by_id(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        user_scope_visibility: str,
    ) -> StoredMemory | None: ...

    @abstractmethod
    async def update(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        changes: dict[str, Any],
    ) -> StoredMemory | None: ...

    @abstractmethod
    async def delete(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        trace_id: str,
        producer_version: str,
    ) -> bool: ...

    @abstractmethod
    async def create_session(
        self, *, session: Session
    ) -> tuple[Session, bool]: ...

    @abstractmethod
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
    ) -> WipeResult: ...

    @abstractmethod
    async def sweep_expired(self, *, batch_size: int) -> int: ...

    async def consolidation_candidates(
        self,
        *,
        max_importance: float,
        min_age_seconds: float,
        batch_size: int,
    ) -> list[StoredMemory]:
        """Return low-importance, old, currently-valid memories to consolidate/forget.

        Default impl returns nothing (safe no-op); the in-memory + PG repos override it.
        Cross-tenant batch read (no app.tenant_id) — used only by the opt-in routine.
        """
        return []

    async def soft_delete_to_audit(
        self,
        *,
        memory: StoredMemory,
        action: str,
        reason: str | None,
        summary_memory_id: str | None,
    ) -> bool:
        """Snapshot ``memory`` to the audit trail then remove it (soft-delete).

        Default impl is a no-op (returns False) so the base contract stays safe; the
        concrete repos implement the actual snapshot + delete.
        """
        return False


# =====================================================================================
# In-memory repository (deterministic; tests + db_pool=None degradation)
# =====================================================================================
class InMemoryRepository(MemoryRepository):
    """A process-local repository with identical semantics to the Postgres one.

    Stores everything in dicts keyed by tenant. Cross-principal isolation is enforced by
    the SAME ``scoping.can_view`` predicate the SQL uses, so a leak here is a leak there.
    """

    def __init__(
        self,
        *,
        default_visibility: str = "isolated",
        contradiction_enabled: bool = False,
        contradiction_sim_min: float = 0.80,
    ) -> None:
        self._memories: dict[str, StoredMemory] = {}
        self._sessions: dict[tuple[str, str, str, str], Session] = {}
        self._wipe_log: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []  # soft-delete / supersession audit trail
        self.events: list[dict[str, Any]] = []  # captured outbox events (test introspection)
        # ── B7: associative edges (src_id -> list of (dst_id, relation, weight)) ──────
        self._links: dict[str, list[tuple[str, str, float]]] = {}
        self._tenant_visibility: dict[str, str] = {}
        self._tenant_dedup: dict[str, float] = {}
        self._default_visibility = default_visibility
        # Contradiction/supersession toggle (defaults OFF -> today's behavior unchanged).
        self.contradiction_enabled = contradiction_enabled
        self.contradiction_sim_min = contradiction_sim_min

    # ── tenant config ────────────────────────────────────────────────────────────
    def set_tenant_visibility(self, tenant_id: str, visibility: str) -> None:
        self._tenant_visibility[tenant_id] = visibility

    def set_tenant_dedup_threshold(self, tenant_id: str, threshold: float) -> None:
        self._tenant_dedup[tenant_id] = threshold

    async def get_tenant_visibility(self, tenant_id: str) -> str:
        return self._tenant_visibility.get(tenant_id, self._default_visibility)

    async def get_tenant_dedup_threshold(self, tenant_id: str, default: float) -> float:
        return self._tenant_dedup.get(tenant_id, default)

    async def resource_usage(
        self, tenant_id: str, principal_type: str, principal_id: str
    ) -> tuple[int, int]:
        count = 0
        total_bytes = 0
        for m in self._memories.values():
            if (
                m.tenant_id == tenant_id
                and m.principal_type == principal_type
                and m.principal_id == principal_id
            ):
                count += 1
                total_bytes += len(m.content.encode("utf-8"))
        return count, total_bytes

    # ── store (with dedup-bump) ───────────────────────────────────────────────────
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
        # Dedup: find the nearest same-principal neighbour; >= threshold -> bump-only.
        # Also collect all same-principal neighbours + their cosine for B7 link decisions.
        best: StoredMemory | None = None
        best_sim = -1.0
        neighbours: list[tuple[str, float]] = []
        for m in self._memories.values():
            if (
                m.tenant_id == memory.tenant_id
                and m.principal_type == memory.principal_type
                and m.principal_id == memory.principal_id
            ):
                sim = cosine_similarity(memory.vector, m.vector)
                neighbours.append((m.id, sim))
                if sim > best_sim:
                    best_sim = sim
                    best = m
        if best is not None and best_sim >= dedup_threshold:
            best.last_accessed_at = _now()
            best.score += 1.0
            self.events.append(
                {"topic": "cypherx.memory.stored", "tenant_id": memory.tenant_id,
                 "memory_id": best.id, "deduped": True}
            )
            return StoreResult(memory=best, deduped=True)

        # ── Contradiction / temporal validity (flag-guarded; OFF -> skipped) ──────────
        # If the nearest same-principal neighbour conflicts (same subject, asserted value,
        # but not an exact dup), mark it SUPERSEDED by the new memory (keep the old row).
        if self.contradiction_enabled and best is not None:
            from .contradiction import is_contradiction

            if is_contradiction(
                new_content=memory.content,
                prior_content=best.content,
                cosine_similarity=best_sim,
                sim_min=self.contradiction_sim_min,
                dedup_threshold=dedup_threshold,
            ):
                now = _now()
                best.valid_until = now
                best.superseded_by_id = memory.id
                self.audit.append(
                    {"action": "superseded", "tenant_id": memory.tenant_id,
                     "memory_id": best.id, "summary_memory_id": memory.id,
                     "principal_type": best.principal_type, "principal_id": best.principal_id}
                )

        self._memories[memory.id] = memory
        # ── B7: write associative edges to the nearest related neighbours (flag-guarded) ──
        if linking_enabled:
            from .linking import LinkCandidate, decide_links

            decisions = decide_links(
                [LinkCandidate(memory_id=mid, similarity=sim) for mid, sim in neighbours],
                sim_min=linking_sim_min, dedup_threshold=dedup_threshold,
                max_neighbors=linking_max_neighbors,
            )
            if decisions:
                self._links.setdefault(memory.id, [])
                for d in decisions:
                    # Undirected association: index both directions so a 1-hop walk from
                    # either endpoint finds the other.
                    self._links[memory.id].append((d.dst_memory_id, d.relation, d.weight))
                    self._links.setdefault(d.dst_memory_id, []).append(
                        (memory.id, d.relation, d.weight)
                    )
        self.events.append(
            {"topic": "cypherx.memory.stored", "tenant_id": memory.tenant_id,
             "memory_id": memory.id, "deduped": False}
        )
        return StoreResult(memory=memory, deduped=False)

    # ── search ────────────────────────────────────────────────────────────────────
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
        now = _now()

        def _visible(m: StoredMemory) -> bool:
            if m.tenant_id != tenant_id:
                return False
            if m.expires_at is not None and m.expires_at <= now:
                return False
            # Temporal validity (flag-guarded): hide superseded memories by default.
            if current_only and m.valid_until is not None and m.valid_until <= now:
                return False
            # VISIBILITY — the leak guard. A non-owner only ever sees tenant_shared under
            # the 'tenant' policy; principal_only never crosses.
            if not scoping.can_view(
                caller_type=caller_type, caller_id=caller_id, owner_type=m.principal_type,
                owner_id=m.principal_id, memory_scope=m.scope,
                user_scope_visibility=user_scope_visibility,
            ):
                return False
            is_owner = m.principal_type == caller_type and m.principal_id == caller_id
            if not include_shared and not is_owner:
                return False
            if type_filter is not None and m.type != type_filter:
                return False
            if tags_filter and not set(tags_filter).issubset(set(m.tags)):
                return False
            # Optional richer-scope filters (additive; only narrow when provided).
            if session_scope_id is not None and m.session_scope_id != session_scope_id:
                return False
            return not (agent_scope_id is not None and m.agent_scope_id != agent_scope_id)

        candidates: list[StoredMemory] = [m for m in self._memories.values() if _visible(m)]

        comps: dict[str, float] = {}
        if scoring_enabled:
            # Composite re-rank (Generative Agents / ACT-R). The candidate SET is unchanged;
            # only the order differs. Recency + access_count use the PRIOR values (captured
            # before this retrieval bumps them).
            weights = scoring_weights or ScoringWeights()
            comps = {
                m.id: composite_score(
                    cosine=cosine_similarity(query_vector, m.vector),
                    importance=m.importance_score,
                    reference=(m.last_retrieved_at or m.last_accessed_at or m.created_at),
                    now=now, weights=weights, access_count=m.access_count,
                )
                for m in candidates
            }

        if mmr_enabled:
            # B6: diversity re-rank of the fetched window. Fail-soft to the base order.
            try:
                scored = mmr_rerank(
                    candidates, query_vector, lambda_mult=mmr_lambda, top_k=top_k
                )
            except Exception:  # noqa: BLE001
                scored = sorted(
                    candidates, key=lambda m: cosine_similarity(query_vector, m.vector),
                    reverse=True,
                )[:top_k]
        elif scoring_enabled:
            scored = sorted(candidates, key=lambda m: comps[m.id], reverse=True)[:top_k]
        else:
            scored = sorted(
                candidates,
                key=lambda m: cosine_similarity(query_vector, m.vector),
                reverse=True,
            )[:top_k]

        # ── B7: bounded 1-hop, embedding-free link expansion (flag-guarded; fail-soft) ──
        # Walk edges from the ranked top_k and APPEND visible linked memories the single-shot
        # cosine missed (a memory relevant by association but far from the query vector). Only
        # ADDS to the result; never drops a vector row, so single-hop recall can't regress.
        if linking_enabled and scored:
            try:
                have = {m.id for m in scored}
                added = 0
                for seed in list(scored):
                    for dst_id, _rel, _w in self._links.get(seed.id, []):
                        if added >= link_expansion_limit:
                            break
                        if dst_id in have:
                            continue
                        linked = self._memories.get(dst_id)
                        if linked is not None and _visible(linked):
                            scored.append(linked)
                            have.add(dst_id)
                            added += 1
            except Exception:  # noqa: BLE001 — expansion is additive: fail soft to vector set
                pass

        out: list[StoredMemory] = []
        for m in scored:
            m.last_accessed_at = now  # inline bump on retrieval
            m.last_retrieved_at = now  # recency input for the composite re-rank
            m.access_count += 1  # B4: retrieval-frequency reinforcement
            m.similarity = cosine_similarity(query_vector, m.vector)
            if m.id in comps:
                m.composite = comps[m.id]
            out.append(m)
        return out

    # ── by-id ─────────────────────────────────────────────────────────────────────
    async def get_by_id(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        user_scope_visibility: str,
    ) -> StoredMemory | None:
        m = self._memories.get(memory_id)
        if m is None or m.tenant_id != tenant_id:
            return None
        if not scoping.can_view(
            caller_type=caller_type,
            caller_id=caller_id,
            owner_type=m.principal_type,
            owner_id=m.principal_id,
            memory_scope=m.scope,
            user_scope_visibility=user_scope_visibility,
        ):
            return None  # 404 anti-existence-leak: invisible == does not exist
        return m

    async def update(
        self,
        *,
        tenant_id: str,
        caller_type: str,
        caller_id: str,
        memory_id: str,
        changes: dict[str, Any],
    ) -> StoredMemory | None:
        m = self._memories.get(memory_id)
        # Mutation is OWNER-ONLY (a non-owner gets a 404, never a 403).
        if (
            m is None
            or m.tenant_id != tenant_id
            or m.principal_type != caller_type
            or m.principal_id != caller_id
        ):
            return None
        if "content" in changes and changes["content"] is not None:
            m.content = changes["content"]
        if "scope" in changes and changes["scope"] is not None:
            m.scope = changes["scope"]
        if "tags" in changes and changes["tags"] is not None:
            m.tags = changes["tags"]
        if "metadata" in changes and changes["metadata"] is not None:
            m.metadata = changes["metadata"]
        if "expires_at" in changes:
            m.expires_at = changes["expires_at"]
        m.last_accessed_at = _now()
        return m

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
        m = self._memories.get(memory_id)
        if (
            m is None
            or m.tenant_id != tenant_id
            or m.principal_type != caller_type
            or m.principal_id != caller_id
        ):
            return False
        del self._memories[memory_id]
        self.events.append(
            {"topic": "cypherx.memory.deleted", "tenant_id": tenant_id, "memory_id": memory_id}
        )
        return True

    # ── sessions ───────────────────────────────────────────────────────────────────
    async def create_session(self, *, session: Session) -> tuple[Session, bool]:
        # Key INCLUDES the principal: same session_id under a DIFFERENT principal is a
        # cross-principal collision (the API turns the second-principal case into a 409).
        for existing in self._sessions.values():
            if existing.tenant_id == session.tenant_id and existing.session_id == session.session_id:
                same_principal = (
                    existing.principal_type == session.principal_type
                    and existing.principal_id == session.principal_id
                )
                # idempotent create for the SAME principal; collision otherwise.
                return existing, same_principal
        key = (session.tenant_id, session.session_id, session.principal_type, session.principal_id)
        self._sessions[key] = session
        return session, True

    # ── GDPR bulk wipe (log + delete + event, atomic by construction here) ─────────
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
        to_delete = [
            mid
            for mid, m in self._memories.items()
            if m.tenant_id == tenant_id
            and m.principal_type == principal_type
            and m.principal_id == principal_id
        ]
        for mid in to_delete:
            del self._memories[mid]
        # remove sessions too
        for key in [
            k
            for k, s in self._sessions.items()
            if s.tenant_id == tenant_id
            and s.principal_type == principal_type
            and s.principal_id == principal_id
        ]:
            del self._sessions[key]
        wipe_log_id = str(uuid.uuid4())
        self._wipe_log.append(
            {"id": wipe_log_id, "tenant_id": tenant_id, "principal_type": principal_type,
             "principal_id": principal_id, "deleted_count": len(to_delete), "reason": reason,
             "requested_by": requested_by}
        )
        self.events.append(
            {"topic": "cypherx.memory.gdpr.wiped", "tenant_id": tenant_id,
             "principal_id": principal_id, "deleted_count": len(to_delete),
             "wipe_log_id": wipe_log_id}
        )
        return WipeResult(deleted_count=len(to_delete), wipe_log_id=wipe_log_id)

    async def sweep_expired(self, *, batch_size: int) -> int:
        now = _now()
        expired = [
            mid for mid, m in self._memories.items() if m.expires_at is not None and m.expires_at <= now
        ][:batch_size]
        for mid in expired:
            del self._memories[mid]
        return len(expired)

    # ── Consolidation / forgetting (opt-in routine) ────────────────────────────────
    async def consolidation_candidates(
        self, *, max_importance: float, min_age_seconds: float, batch_size: int
    ) -> list[StoredMemory]:
        now = _now()
        out: list[StoredMemory] = []
        for m in self._memories.values():
            if m.importance_score > max_importance:
                continue
            if m.valid_until is not None and m.valid_until <= now:
                continue  # already superseded
            age = (now - (m.last_retrieved_at or m.last_accessed_at or m.created_at)).total_seconds()
            if age < min_age_seconds:
                continue
            out.append(m)
            if len(out) >= batch_size:
                break
        return out

    async def soft_delete_to_audit(
        self, *, memory: StoredMemory, action: str, reason: str | None,
        summary_memory_id: str | None,
    ) -> bool:
        if memory.id not in self._memories:
            return False
        self.audit.append(
            {"action": action, "tenant_id": memory.tenant_id, "memory_id": memory.id,
             "principal_type": memory.principal_type, "principal_id": memory.principal_id,
             "reason": reason, "summary_memory_id": summary_memory_id,
             "snapshot": {"content": memory.content, "type": memory.type}}
        )
        del self._memories[memory.id]
        return True


def new_memory(
    *,
    tenant_id: str,
    principal_type: str,
    principal_id: str,
    scope: str,
    type: str,
    tags: list[str],
    content: str,
    metadata: dict[str, Any],
    vector: list[float],
    session_id: str | None,
    ttl_seconds: int | None,
    importance_score: float | None = None,
    session_scope_id: str | None = None,
    agent_scope_id: str | None = None,
) -> StoredMemory:
    """Build a fresh StoredMemory with server-assigned id/timestamps.

    ``importance_score`` defaults to the deterministic write-time heuristic; it is stored
    regardless of MEMORY_SCORING_ENABLED (cheap + additive) but only AFFECTS ranking when
    that flag is on. ``session_scope_id`` / ``agent_scope_id`` are optional richer-scope
    fields (additive; default None reproduces today's behavior).
    """
    now = _now()
    expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
    if importance_score is None:
        importance_score = heuristic_importance(content, memory_type=type)
    return StoredMemory(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_id=principal_id,
        scope=scope,
        type=type,
        tags=list(tags),
        content=content,
        metadata=dict(metadata),
        vector=vector,
        session_id=session_id,
        score=1.0,
        created_at=now,
        last_accessed_at=now,
        expires_at=expires_at,
        importance_score=importance_score,
        last_retrieved_at=None,
        valid_until=None,
        superseded_by_id=None,
        session_scope_id=session_scope_id,
        agent_scope_id=agent_scope_id,
    )


def to_wire(m: StoredMemory, *, deduped: bool | None = None) -> dict[str, Any]:
    """Map a StoredMemory to the wire MemoryRecord dict (omits the raw vector)."""
    similarity = None
    if m.similarity is not None:
        # cosine in [-1,1] -> clamp to [0,1] for a friendlier score
        similarity = round(max(0.0, min(1.0, (m.similarity + 1.0) / 2.0)), 6)
    out: dict[str, Any] = {
        "id": m.id,
        "principal_type": m.principal_type,
        "principal_id": m.principal_id,
        "scope": m.scope,
        "type": m.type,
        "tags": m.tags,
        "content": m.content,
        "metadata": m.metadata,
        "session_id": m.session_id,
        "score": m.score,
        "created_at": _iso(m.created_at),
        "last_accessed_at": _iso(m.last_accessed_at),
        "expires_at": _iso(m.expires_at),
        # ── Additive fields (always present; defaults preserve today's semantics) ─────
        "importance_score": round(m.importance_score, 6),
        "last_retrieved_at": _iso(m.last_retrieved_at),
        "valid_until": _iso(m.valid_until),
        "superseded_by_id": m.superseded_by_id,
        "access_count": int(m.access_count),
    }
    if m.session_scope_id is not None:
        out["session_scope_id"] = m.session_scope_id
    if m.agent_scope_id is not None:
        out["agent_scope_id"] = m.agent_scope_id
    if m.similarity is not None:
        out["similarity"] = similarity
    if m.composite is not None:
        out["composite_score"] = round(m.composite, 6)
    if deduped is not None:
        out["deduped"] = deduped
    return out
