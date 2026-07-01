"""Prometheus metrics for the guardrails service."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

checks_total = Counter(
    "guardrails_checks_total",
    "Total guardrail checks handled.",
    labelnames=("direction", "decision"),
)

# SLO histogram buckets for the hot-path check latency (seconds). Tuned around the
# Component-1 budget (single-digit-ms regex/heuristic rules; the 50ms classifier rules).
# Prometheus alert-rule thresholds documented in core/slo.py.
_CHECK_LATENCY_BUCKETS = (
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
)

check_duration_seconds = Histogram(
    "guardrails_check_duration_seconds",
    "End-to-end check duration in seconds.",
    labelnames=("direction",),
    buckets=_CHECK_LATENCY_BUCKETS,
)

# ── Hot-path policy cache (WP07) ────────────────────────────────────────────────
policy_cache_total = Counter(
    "guardrails_policy_cache_total",
    "Policy-cache lookups by outcome ('hit' | 'miss' | 'error' | 'disabled').",
    labelnames=("outcome",),
)

# ── Per-tenant rate limiting + byte quota (WP07) ────────────────────────────────
rate_limit_decisions_total = Counter(
    "guardrails_rate_limit_decisions_total",
    "Rate-limiter decisions by outcome ('allowed' | 'limited' | 'failopen' | 'disabled') "
    "and dimension ('checks' | 'bytes' | 'none').",
    labelnames=("outcome", "dimension"),
)

# ── Post-response persistence queue (WP07) ──────────────────────────────────────
persist_queue_backlog = Gauge(
    "guardrails_persist_queue_backlog",
    "Current depth of the in-process violation/usage persistence queue.",
)
persist_queue_dropped_total = Counter(
    "guardrails_persist_queue_dropped_total",
    "Persistence items dropped because the queue was full (metering loss, not safety).",
)
persist_queue_processed_total = Counter(
    "guardrails_persist_queue_processed_total",
    "Persistence items drained from the queue by outcome ('ok' | 'failed').",
    labelnames=("outcome",),
)

# ── Outbox purge (WP07 — Ops) ───────────────────────────────────────────────────
outbox_purged_total = Counter(
    "guardrails_outbox_purged_total",
    "Published outbox rows deleted by the retention purge job.",
)

# ── Policy authoring + simulation (WP07) ────────────────────────────────────────
policy_writes_total = Counter(
    "guardrails_policy_writes_total",
    "Policy authoring writes by operation ('create' | 'edit' | 'assign') and "
    "outcome ('ok' | 'invalid' | 'not_found' | 'error').",
    labelnames=("operation", "outcome"),
)
simulations_total = Counter(
    "guardrails_simulations_total",
    "Policy simulations by source ('stored' | 'draft') and decision.",
    labelnames=("source", "decision"),
)
simulation_rate_limited_total = Counter(
    "guardrails_simulation_rate_limited_total",
    "Simulate calls rejected (429) by the per-tenant sim/hour limiter.",
)

# ── Redaction-key lifecycle (WP07) ──────────────────────────────────────────────
redaction_keys_retired_total = Counter(
    "guardrails_redaction_keys_retired_total",
    "Redaction keys retired by the grace-window retirement job.",
)

rule_evaluations_timeout_total = Counter(
    "guardrails_rule_evaluations_timeout_total",
    "Per-rule evaluation timeouts (fail-mode applied).",
    labelnames=("rule_id",),
)

# ── Real classifier seam / confidence-banded cascade ────────────────────────────
classifier_cascade_total = Counter(
    "guardrails_classifier_cascade_total",
    "Classifier cascade outcomes by stage ('stub_only' | 'remote' | 'remote_fallback').",
    labelnames=("outcome",),
)

# ── Output groundedness / hallucination signal ──────────────────────────────────
groundedness_checks_total = Counter(
    "guardrails_groundedness_checks_total",
    "Output groundedness signals by outcome ('grounded' | 'high_risk').",
    labelnames=("outcome",),
)

# ── Prompt-injection spotlight (instruction-hierarchy defense) ──────────────────
injection_spotlight_total = Counter(
    "guardrails_injection_spotlight_total",
    "Injection spotlight outcomes ('escalated' = untrusted-span hit blocked | 'observed').",
    labelnames=("outcome",),
)

violation_write_failed_total = Counter(
    "guardrails_violation_write_failed_total",
    "Violation/outbox write failures (DB unreachable).",
    labelnames=("reason",),
)

valkey_up = Gauge(
    "guardrails_valkey_up",
    "1 if the last Valkey ping succeeded, 0 otherwise (soft dependency).",
)

revocation_checks_total = Counter(
    "guardrails_revocation_checks_total",
    "Token revocation-mirror lookups by outcome ('clean' | 'revoked' | 'skipped').",
    labelnames=("outcome",),
)
