"""The canonical ingestion model — the unified shape every connector normalizes to.

A connector's ``to_canonical`` turns one source record (a commit, PR, issue, message, …)
into a :class:`CanonicalRecord`: a set of graph NODES, typed EDGES between them, and
optional RAG DOCS (text to embed into a knowledge base). The normalizer upserts the nodes
+ edges into the app-owned graph and ingests the docs into the SharedCore RAG corpus,
linking each chunk back to the originating node as a citation.

NODES are referenced by a stable ``(kind, natural_key)`` pair so edges can wire two nodes
together before either has a database UUID — the normalizer resolves refs to ``entity_id``
at upsert time. ``natural_key`` is the dedup key within ``(tenant, kind)`` (e.g. a repo's
``owner/name``, a person's canonical email/login, a PR's ``repo#number``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EntityKind = Literal[
    "person", "service", "repo", "feature", "decision", "incident", "pr", "ticket", "document",
    # Phase B: a discrete change event + consolidation/reflection summaries.
    "change", "capability", "expertise_summary",
]
EdgeRel = Literal[
    "owns", "authored", "reviewed", "depends_on", "caused", "resolved",
    "mentions", "decided_in", "deployed", "expert_in", "part_of",
    # Phase B: change -> what it touched; summary -> the evidence it summarizes.
    "touched", "summarizes",
]
# Logical RAG KB names (resolved to a kb_id per tenant at first use).
KbName = Literal["eng-code", "eng-conversations", "eng-docs", "eng-incidents"]


@dataclass(frozen=True)
class NodeRef:
    """A reference to a node by its stable identity (resolved to entity_id at upsert)."""

    kind: EntityKind
    natural_key: str


@dataclass
class CanonicalNode:
    """A knowledge-graph node emitted by a connector."""

    kind: EntityKind
    source: str
    natural_key: str
    title: str | None = None
    search_text: str | None = None
    external_id: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    # Optional cross-tool identity handles for a person node (github login / slack uid / …).
    identity_handles: list[tuple[str, str]] = field(default_factory=list)  # (source, handle)

    @property
    def ref(self) -> NodeRef:
        return NodeRef(kind=self.kind, natural_key=self.natural_key)


@dataclass
class CanonicalEdge:
    """A typed relationship between two canonical nodes."""

    rel: EdgeRel
    src: NodeRef
    dst: NodeRef
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RagDoc:
    """A piece of text to embed into a RAG knowledge base, linked back to a node."""

    kb: KbName
    name: str
    content: str
    node: NodeRef
    metadata: dict[str, Any] = field(default_factory=dict)
    source_type: Literal["markdown", "text"] = "markdown"


@dataclass
class CanonicalRecord:
    """One normalized source record: its nodes, edges, and embeddable docs."""

    source: str
    record_type: str
    external_id: str
    content_sha: str
    nodes: list[CanonicalNode] = field(default_factory=list)
    edges: list[CanonicalEdge] = field(default_factory=list)
    docs: list[RagDoc] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
