"""Prometheus metrics (Contract 7 ``/metrics``).

A small, dependency-light metric set covering the request path, downstream SharedCore
calls, the ingestion/extraction pipeline, and the revocation mirror. Histograms (not
summaries) per the platform metrics convention.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Downstream SharedCore calls (label: service, outcome) ─────────────────────
downstream_calls_total = Counter(
    "cypherxa1_downstream_calls_total",
    "Downstream SharedCore calls by service and outcome.",
    ["service", "outcome"],
)

# ── Copilot ────────────────────────────────────────────────────────────────────
copilot_requests_total = Counter(
    "cypherxa1_copilot_requests_total",
    "Copilot ask requests by outcome.",
    ["outcome"],
)
copilot_latency_seconds = Histogram(
    "cypherxa1_copilot_latency_seconds",
    "End-to-end copilot answer latency.",
)

# ── Ingestion + extraction pipeline ───────────────────────────────────────────
ingestion_records_total = Counter(
    "cypherxa1_ingestion_records_total",
    "Canonical records ingested by source and outcome.",
    ["source", "outcome"],
)
extraction_jobs_total = Counter(
    "cypherxa1_extraction_jobs_total",
    "Knowledge-extraction jobs by outcome (completed|skipped|failed).",
    ["outcome"],
)
graph_edges_upserted_total = Counter(
    "cypherxa1_graph_edges_upserted_total",
    "Graph edges upserted by relation.",
    ["rel"],
)
# Phase KG: edges the schema-guided extractor rejected (off-schema / below-floor in drop
# mode) — the measurable signal that hallucinated relations were kept out of the graph.
extraction_edges_rejected_total = Counter(
    "cypherxa1_extraction_edges_rejected_total",
    "Proposed extraction edges rejected (off-schema or below confidence floor).",
)
# Phase KG: duplicate entities merged into their canonical by the resolver.
entity_merges_total = Counter(
    "cypherxa1_entity_merges_total",
    "Duplicate entities merged into a canonical entity by coreference resolution.",
)

# ── Live token revocation mirror (Contract 1 / WP03) ──────────────────────────
revocation_checks_total = Counter(
    "cypherxa1_revocation_checks_total",
    "Verifier-side revocation checks by outcome.",
    ["outcome"],
)
revocation_check_skipped_total = Counter(
    "cypherxa1_revocation_check_skipped_total",
    "Revocation checks skipped (fail-open) due to missing/unreachable Valkey.",
)

# ── Tracing gauge ──────────────────────────────────────────────────────────────
otel_tracing_enabled = Gauge(
    "cypherxa1_otel_tracing_enabled",
    "1 when OTLP span export is wired, else 0.",
)
