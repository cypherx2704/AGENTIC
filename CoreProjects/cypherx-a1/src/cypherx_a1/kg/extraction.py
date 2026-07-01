"""Schema-constrained, QA-gated parsing of an extractor's proposed edges (pure, no I/O).

Turns a raw LLM JSON completion into validated :class:`ProposedEdge` records. Three gates,
all reusable + unit-testable without a DB or an LLM:

  1. SHAPE — tolerant JSON parse; a malformed / wrong-shaped response yields ``[]`` (the
     caller still records the job so it is not retried forever).
  2. SCHEMA — when a :class:`~cypherx_a1.kg.schema.GraphSchema` is supplied, a relation
     outside the ontology (wrong rel, or a target/source kind the rule forbids) is REJECTED
     or FLAGGED (``schema_ok=False``). This cuts hallucinated relations.
  3. CONFIDENCE — an edge below ``floor`` is dropped (``mode='drop'``) or kept-but-flagged
     (``mode='flag'``, default — preserves recall).

Also captures the source SPAN (the evidence quote) per edge for extraction QA + provenance.

This is the engine behind ``extractor._parse_edges``; the extractor keeps a thin backward-
compatible wrapper so existing callers/tests are untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .schema import GraphSchema

# The relations the extractor may emit when NO explicit schema is enforced — today's set
# (extractor._EXTRACTABLE_RELS). Used as the shape allow-list so the default path is unchanged.
DEFAULT_EXTRACTABLE_RELS: frozenset[str] = frozenset(
    {"depends_on", "decided_in", "caused", "resolved", "expert_in", "mentions"}
)
DEFAULT_TARGET_KINDS: frozenset[str] = frozenset(
    {"service", "repo", "feature", "decision", "incident", "person", "document", "pr", "ticket"}
)


@dataclass
class ProposedEdge:
    """One validated edge an extractor proposes, with its QA signals."""

    rel: str
    target_kind: str
    target_key: str
    confidence: float
    evidence: str = ""
    source_span: str = ""
    flagged: bool = False  # below the confidence floor (kept for recall in 'flag' mode)
    schema_ok: bool = True  # passed schema/ontology validation (False ⇒ off-schema, flagged)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rel": self.rel,
            "target_kind": self.target_kind,
            "target_key": self.target_key,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "source_span": self.source_span,
            "flagged": self.flagged,
            "schema_ok": self.schema_ok,
        }


@dataclass
class ParseResult:
    edges: list[ProposedEdge] = field(default_factory=list)
    rejected: int = 0  # items dropped (off-schema in reject mode, or below-floor in drop mode)


def parse_extracted_edges(
    content: str | None,
    *,
    floor: float = 0.0,
    mode: str = "flag",
    allowed_rels: frozenset[str] = DEFAULT_EXTRACTABLE_RELS,
    allowed_target_kinds: frozenset[str] = DEFAULT_TARGET_KINDS,
    schema: GraphSchema | None = None,
    schema_mode: str = "reject",
    source_kind: str | None = None,
) -> ParseResult:
    """Parse + validate an extractor JSON completion into :class:`ProposedEdge` records.

    ``schema`` (optional) enforces the ontology: ``schema_mode='reject'`` drops off-schema
    relations entirely (tighter graph, cuts hallucinations); ``schema_mode='flag'`` keeps
    them with ``schema_ok=False`` so a review queue can inspect them. With ``schema=None``
    only the basic ``allowed_rels`` / ``allowed_target_kinds`` shape allow-list applies —
    exactly today's behavior.
    """
    result = ParseResult()
    if not content:
        return result
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return result
    raw = data.get("edges") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return result

    for item in raw:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("rel", "")).strip()
        kind = str(item.get("target_kind", "")).strip()
        key = str(item.get("target_key", "")).strip()
        # A structurally invalid item (empty rel / kind / key) is always silently skipped —
        # it never had a valid shape, so it is not counted as a schema rejection.
        if not rel or not kind or not key:
            continue

        # Relation/target-kind membership:
        #   * schema=None  -> the basic shape allow-list applies (TODAY'S behavior): an
        #     unknown rel/kind is silently skipped, not counted.
        #   * schema given -> the SCHEMA is the authority. A shaped-but-off-schema relation
        #     (e.g. a hallucinated 'likes') reaches the schema gate and is rejected/flagged
        #     and COUNTED — this is the schema-guided-extraction win.
        if schema is None and (rel not in allowed_rels or kind not in allowed_target_kinds):
            continue

        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5

        # Schema / ontology gate (only when a schema is supplied).
        schema_ok = True
        if schema is not None:
            schema_ok = schema.validate(rel=rel, target_kind=kind, source_kind=source_kind)
            if not schema_ok and schema_mode == "reject":
                result.rejected += 1
                continue

        flagged = confidence < floor
        if flagged and mode == "drop":
            result.rejected += 1
            continue

        evidence = str(item.get("evidence", ""))[:500]
        # source_span: prefer an explicit span field, else reuse the evidence quote.
        span = str(item.get("source_span", "") or item.get("span", "") or evidence)[:1000]
        result.edges.append(
            ProposedEdge(
                rel=rel,
                target_kind=kind,
                target_key=key,
                confidence=confidence,
                evidence=evidence,
                source_span=span,
                flagged=flagged,
                schema_ok=schema_ok,
            )
        )
    return result
