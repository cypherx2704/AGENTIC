"""Reusable internal knowledge-graph library (extraction + resolution + schema).

A small, dependency-free toolkit factored so a future SHARED knowledge-graph service is a
lift-out, not a rewrite. Everything here is PURE (no DB, no I/O, no settings, no LLM) so it
is trivially unit-testable and portable:

  * :mod:`cypherx_a1.kg.schema`     — the ontology (allowed entity kinds + relation set +
    per-relation src/dst type constraints) and schema-guided extraction validation. Cuts
    hallucinated relations by rejecting/flagging anything outside the allowed schema.
  * :mod:`cypherx_a1.kg.resolution` — type-aware coreference helpers (normalize a surface
    form, generate match variants, decide whether two mentions co-refer) so 'J. Smith' and
    'John Smith' resolve to one entity.
  * :mod:`cypherx_a1.kg.extraction` — schema-constrained, QA-gated parsing of an extractor's
    proposed edges (confidence floor + source-span capture).

The cypherx-a1 app wires these into its DB-backed normalizer / extractor / graph_repo; the
DB + identity + RLS stay app-owned (graph is the crown jewel and is NOT pushed into
SharedCore). The boundary is deliberately the pure logic only.
"""

from __future__ import annotations

from .extraction import ProposedEdge, parse_extracted_edges
from .resolution import are_coreferent, mention_variants, normalize_mention
from .schema import DEFAULT_SCHEMA, GraphSchema, RelationRule

__all__ = [
    "DEFAULT_SCHEMA",
    "GraphSchema",
    "ProposedEdge",
    "RelationRule",
    "are_coreferent",
    "mention_variants",
    "normalize_mention",
    "parse_extracted_edges",
]
