"""Public API wire models (Contract 2 / 13).

All request models set ``extra="forbid"`` so identity/reserved keys (tenant_id, agent_id,
trace_id, …) in a body are rejected with 422 — identity comes only from the JWT. Responses
are plain models. ``Citation`` is the provenance unit every answer/tool result carries.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Resp(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ── Provenance ────────────────────────────────────────────────────────────────
class Citation(_Resp):
    kind: Literal["entity", "chunk"]
    title: str
    source: str | None = None
    uri: str | None = None
    entity_id: str | None = None
    entity_kind: str | None = None
    natural_key: str | None = None
    doc_id: str | None = None
    chunk_id: str | None = None
    score: float | None = None
    snippet: str | None = None


# ── Copilot ───────────────────────────────────────────────────────────────────
class AskRequest(_Req):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=128)
    top_k: int = Field(default=8, ge=1, le=50)


class AskResponse(_Resp):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    used: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    duration_ms: int | None = None


# ── Connectors (ingest / extract triggers) ────────────────────────────────────
class SyncRequest(_Req):
    # Optional "owner/name" seed for a live pull; ignored in mock mode.
    repo: str | None = Field(default=None, max_length=140)
    mode: Literal["full", "incremental"] = "full"


class SyncResponse(_Resp):
    connector: str
    records_seen: int
    records_new: int
    nodes_upserted: int
    edges_upserted: int
    docs_ingested: int
    skipped_duplicate: int
    errors: int


class ExtractResponse(_Resp):
    nodes_seen: int
    nodes_extracted: int
    edges_added: int
    failed: int
    # Phase B: populated when ?consolidate=true also runs the reflection/consolidation pass.
    summaries_written: int = 0
    persons_consolidated: int = 0
    # Phase C: recency-decayed Degree-of-Knowledge expert_in edges (re)written in the same pass.
    expert_edges_written: int = 0


# ── Graph query surface (also the MCP server's backing API) ───────────────────
class TargetRequest(_Req):
    target: str = Field(min_length=1, max_length=300)


class WhatBreaksRequest(_Req):
    target: str = Field(min_length=1, max_length=300)
    max_hops: int = Field(default=3, ge=1, le=6)
    # Phase KG (additive, optional): ISO 8601 timestamp to read the graph AS OF that instant
    # (bi-temporal time-travel). Omitted (None, default) ⇒ the current valid slice — today's
    # behavior. Used by /v1/graph/neighbors.
    as_of: str | None = Field(default=None, max_length=40)


class TopicRequest(_Req):
    topic: str = Field(min_length=1, max_length=300)


class ActivityRequest(_Req):
    target: str = Field(min_length=1, max_length=300)  # a repo (owner/name) or a person
    since: str | None = Field(default=None, max_length=40)  # ISO 8601 timestamp
    until: str | None = Field(default=None, max_length=40)


class GraphAnswer(_Resp):
    """A read-only graph query result: structured items + citations (no LLM)."""

    items: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    trace_id: str | None = None
