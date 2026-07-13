"""Request/response models for the Memory API (Component 1).

Field names are wire-stable. ``scope`` governs cross-principal visibility:

* ``principal_only`` (DEFAULT) — the memory is private to its owning principal. A
  different principal in the same tenant can NEVER retrieve it (THE cross-end-user leak
  regression). This is the safe default.
* ``tenant_shared``           — visible to any principal in the tenant (subject to the
  tenant's ``user_scope_visibility`` policy).

Identity (tenant_id / principal) is taken from the JWT, NEVER the body (Contract 13);
there are no tenant_id / principal_id fields on the request models.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryScope = Literal["principal_only", "tenant_shared"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Store ────────────────────────────────────────────────────────────────────────
class StoreMemoryRequest(_Base):
    content: str = Field(min_length=1)
    type: str = Field(default="note", max_length=64)
    scope: MemoryScope = "principal_only"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional session this memory belongs to (sessions are keyed by principal).
    session_id: str | None = None
    # Optional TTL in seconds; when set the memory is swept once expired.
    ttl_seconds: int | None = Field(default=None, ge=1)
    # ── Additive optional fields (defaults reproduce today's behavior) ────────────────
    # Caller-supplied importance in [0,1]; when omitted the server uses its deterministic
    # write-time heuristic (and, if MEMORY_IMPORTANCE_LLM_ENABLED, may grade it).
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    # Optional richer scope (additive; do NOT relax the principal anti-leak rule).
    session_scope_id: str | None = Field(default=None, max_length=128)
    agent_scope_id: str | None = Field(default=None, max_length=128)


class MemoryRecord(_Base):
    id: str
    principal_type: str
    principal_id: str
    scope: MemoryScope
    type: str
    tags: list[str]
    content: str
    metadata: dict[str, Any]
    session_id: str | None = None
    score: float
    created_at: str
    last_accessed_at: str
    expires_at: str | None = None
    # ── Additive scoring / validity / richer-scope fields (defaults are inert) ────────
    # Normalized [0,1] write-time importance (used in ranking only when
    # MEMORY_SCORING_ENABLED). Always reported so a caller can introspect it.
    importance_score: float | None = None
    # When this memory was last RETURNED by a search (recency input). None = never.
    last_retrieved_at: str | None = None
    # Temporal validity: None = currently valid; set when superseded.
    valid_until: str | None = None
    superseded_by_id: str | None = None
    # How many times this memory has been RETURNED by a search (ACT-R frequency input).
    access_count: int | None = None
    # Optional richer scope (only present when set on the memory).
    session_scope_id: str | None = None
    agent_scope_id: str | None = None
    # Only present on a search response: the cosine similarity to the query (0..1).
    similarity: float | None = None
    # The composite re-rank score (only present when MEMORY_SCORING_ENABLED).
    composite_score: float | None = None
    # True when the store request hit a near-duplicate and bumped an existing row.
    deduped: bool | None = None


# ── Update (PUT) ─────────────────────────────────────────────────────────────────
class UpdateMemoryRequest(_Base):
    # Mutable fields only. Immutable fields (id, principal, type, created_at) are rejected
    # by the endpoint if present — the model forbids unknown fields so they 422 anyway.
    content: str | None = Field(default=None, min_length=1)
    scope: MemoryScope | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)


# ── Search ───────────────────────────────────────────────────────────────────────
class SearchMemoryRequest(_Base):
    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1)
    type: str | None = None
    tags: list[str] | None = None
    # When True, also include tenant_shared memories owned by OTHER principals (subject to
    # the tenant's user_scope_visibility policy). Default False = only what the caller owns
    # plus tenant_shared the policy allows.
    include_shared: bool = True
    # ── Additive optional filters (None = no extra narrowing, today's behavior) ───────
    session_scope_id: str | None = Field(default=None, max_length=128)
    agent_scope_id: str | None = Field(default=None, max_length=128)
    # Per-request override for "current only" temporal-validity filtering. None defers to
    # the server flag (MEMORY_SEARCH_CURRENT_ONLY); True/False overrides it for this call.
    include_superseded: bool | None = None


class SearchMemoryResponse(_Base):
    results: list[MemoryRecord]
    count: int


# ── Sessions ─────────────────────────────────────────────────────────────────────
class CreateSessionRequest(_Base):
    session_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(_Base):
    session_id: str
    principal_type: str
    principal_id: str
    title: str | None = None
    metadata: dict[str, Any]
    created_at: str


# ── GDPR ─────────────────────────────────────────────────────────────────────────
class GdprWipeRequest(_Base):
    # The principal to wipe. Defaults to the CALLER's own principal when omitted; an
    # admin (mem:write) may target another principal in the same tenant by id.
    principal_type: str | None = None
    principal_id: str | None = None
    reason: str | None = Field(default=None, max_length=512)


class GdprWipeResponse(_Base):
    principal_type: str
    principal_id: str
    deleted_count: int
    wipe_log_id: str
