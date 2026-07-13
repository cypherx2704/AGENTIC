"""API request/response models (Component 1, 2, 4, 5c).

Identity fields (tenant_id, agent_id, trace_id, ...) are NEVER accepted in a request
body (Contract 13 anti-pattern guard) — ``extra="forbid"`` rejects them outright, and
the auth layer derives identity from the JWT chain only.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


CHUNKING_STRATEGIES = ("fixed", "sentence")  # first-cycle (semantic/recursive are 📋)
SOURCE_TYPES = ("pdf", "markdown", "text", "html", "url")
PERMISSIONS = ("read", "query", "ingest", "write", "admin")
PRINCIPAL_TYPES = ("agent", "api_key", "user", "role", "tenant")


# ── Knowledge bases (Component 1) ───────────────────────────────────────────────
class CreateKbRequest(_Base):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    chunking_strategy: Literal["fixed", "sentence"] = "sentence"
    chunk_size: int = Field(default=512, ge=1, le=8192)
    chunk_overlap: int = Field(default=50, ge=0, le=4096)
    embedding_model_alias: str = "embed"
    # When true, the default (tenant,'*') ACL is OMITTED — only the creator/explicit
    # ACL adds can access the KB.
    private: bool = False


class KbResponse(_Base):
    kb_id: str
    tenant_id: str
    name: str
    description: str | None = None
    chunking_strategy: str
    chunk_size: int
    chunk_overlap: int
    embedding_model_alias: str
    embedding_model_resolved: str
    embedding_dim: int
    status: str
    created_at: str
    updated_at: str


class KbStatusResponse(_Base):
    kb_id: str
    document_count: int
    chunk_count: int
    pending_docs: int
    failed_docs: int
    last_updated_at: str | None = None


# ── Query (Component 4) ─────────────────────────────────────────────────────────
class QueryRequest(_Base):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    filters: dict[str, Any] | None = None
    # 'dense' (default) keeps the two-pass HNSW path EXACTLY as-is. 'hybrid' fuses dense +
    # lexical (Postgres tsvector / ts_rank_cd) via Reciprocal Rank Fusion; 'sparse' is the
    # lexical leg alone. The lexical legs rely on migration 0003's content_tsv column.
    search_mode: Literal["dense", "hybrid", "sparse"] = "dense"
    ef_search: int | None = Field(default=None, ge=1)
    # OPTIONAL cross-encoder rerank. No-op unless RAG_RERANK_ENABLED is on (then the candidate
    # pool is reranked via llms-gateway /v1/rerank and the top_k returned). Default false ⇒
    # today's behaviour. ``min_score`` is NOT re-applied to fused/reranked scores (they are
    # rank-fusion / relevance scores, not cosine similarities) — see the query handler.
    rerank: bool = False
    # OPTIONAL multi-hop query decomposition. No-op unless RAG_DECOMPOSE_ENABLED is on (then the
    # query is split into ≤ decompose_max_subquestions sub-questions, retrieved per sub-question,
    # unioned+deduped, and fed to the rerank stage). Default false ⇒ today's single-query path.
    decompose: bool = False
    # OPTIONAL multi-query expansion (RAG-Fusion). No-op unless RAG_MULTIQUERY_ENABLED is on (then
    # the query is expanded into paraphrases, retrieved per variant, and fused with app-level RRF).
    # A recall lever. Default false ⇒ today's single-query path.
    multi_query: bool = False


class QueryHitSource(_Base):
    name: str
    uri: str | None = None


class QueryHit(_Base):
    chunk_id: str
    doc_id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: QueryHitSource


class QueryResponse(_Base):
    results: list[QueryHit]
    query_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    duration_ms: int


# ── Inline ingest (Component 2) ─────────────────────────────────────────────────
class InlineIngestRequest(_Base):
    name: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    source_type: Literal["markdown", "text"] = "markdown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentResponse(_Base):
    doc_id: str
    kb_id: str
    name: str
    source_type: str
    source_uri: str | None = None
    status: str
    attempts: int = 0
    error_msg: str | None = None
    created_at: str
    completed_at: str | None = None


class DocumentListResponse(_Base):
    documents: list[DocumentResponse]
    next_offset: int | None = None


# ── Presigned upload / finalize (Component 2) ───────────────────────────────────
class UploadUrlRequest(_Base):
    filename: str = Field(min_length=1, max_length=500)
    size_bytes: int = Field(ge=1)
    content_type: str

    @field_validator("filename")
    @classmethod
    def _no_path_traversal(cls, v: str) -> str:
        if "/" in v or "\\" in v or ".." in v:
            raise ValueError("filename must not contain path separators")
        return v


class UploadUrlResponse(_Base):
    upload_url: str
    doc_id: str
    fields: dict[str, str] = Field(default_factory=dict)
    expires_in: int


class FinalizeRequest(_Base):
    doc_id: str


# ── KB ACLs (Component 5c) ──────────────────────────────────────────────────────
class AclRow(_Base):
    principal_type: Literal["agent", "api_key", "user", "role", "tenant"]
    principal_id: str
    permissions: list[Literal["read", "query", "ingest", "write", "admin"]] = Field(min_length=1)
    expires_at: str | None = None


class AclResponse(AclRow):
    kb_id: str
    created_by: str
    created_at: str


class AclListResponse(_Base):
    acls: list[AclResponse]


class ReplaceAclsRequest(_Base):
    acls: list[AclRow] = Field(default_factory=list)
