"""Prometheus metrics for the tool-flow-bridge service."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── MCP invoke (agent -> bridge -> Node-RED) ─────────────────────────────────────
invoke_total = Counter(
    "tfb_invoke_total",
    "Total MCP tool invocations handled, by tool slug + status.",
    labelnames=("slug", "status"),
)

invoke_duration_seconds = Histogram(
    "tfb_invoke_duration_seconds",
    "End-to-end /w/<slug>/mcp/v1/invoke duration in seconds.",
)

invoke_rejected_total = Counter(
    "tfb_invoke_rejected_total",
    "Invocations rejected before/after dispatch, by reason "
    "(scope_denied | schema_invalid | output_too_large | not_found | nodered_error).",
    labelnames=("reason",),
)

manifest_served_total = Counter(
    "tfb_manifest_served_total",
    "GET /w/<slug>/manifest responses by outcome (200 = body returned, 304 = ETag matched).",
    labelnames=("status",),
)

# ── Publish pipeline (user -> bridge -> registry) ────────────────────────────────
publish_total = Counter(
    "tfb_publish_total",
    "Publish/unpublish operations by action + status.",
    labelnames=("action", "status"),
)

publish_duration_seconds = Histogram(
    "tfb_publish_duration_seconds",
    "Publish operation duration in seconds.",
)

# ── Node-RED outbound (adapter + admin) ──────────────────────────────────────────
nodered_invoke_total = Counter(
    "tfb_nodered_invoke_total",
    "Outbound Node-RED HTTP-In webhook calls by status (ok | client_error | server_error | timeout).",
    labelnames=("status",),
)

nodered_admin_total = Counter(
    "tfb_nodered_admin_total",
    "Outbound Node-RED Admin API calls by operation + status.",
    labelnames=("op", "status"),
)

# ── Tenant-runtime provisioner ───────────────────────────────────────────────────
provision_total = Counter(
    "tfb_provision_total",
    "Tenant Node-RED provisioning operations by mode + status.",
    labelnames=("mode", "status"),
)

# ── Registry client ──────────────────────────────────────────────────────────────
registry_call_total = Counter(
    "tfb_registry_call_total",
    "Outbound Tool Registry calls by operation + status.",
    labelnames=("op", "status"),
)

# ── Valkey (soft dependency) ─────────────────────────────────────────────────────
valkey_up = Gauge(
    "tfb_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when Valkey is unavailable (soft dependency).",
)

# ── Postgres (hard dependency) ───────────────────────────────────────────────────
db_up = Gauge(
    "tfb_db_up",
    "1 when the last Postgres readiness ping succeeded, 0 otherwise.",
)

# ── Token revocation (WP03 verifier-side mirror) ─────────────────────────────────
revocation_check_skipped_total = Counter(
    "tfb_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable (fail-open).",
)

revocation_rejected_total = Counter(
    "tfb_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation mirror.",
    labelnames=("rule",),
)

# ── Per-tenant rate limiting (fail-open) ─────────────────────────────────────────
rate_limit_rejected_total = Counter(
    "tfb_rate_limit_rejected_total",
    "Requests rejected (429 RATE_LIMIT_EXCEEDED) by the pre-request fixed-window check.",
    labelnames=("dimension",),
)

rate_limit_failopen_total = Counter(
    "tfb_rate_limit_failopen_total",
    "Rate-limit checks that FAILED OPEN (allowed) because Valkey was unavailable or errored.",
    labelnames=("op",),
)

# ── Idempotency (Contract-9 style) ──────────────────────────────────────────────
idempotency_replayed_total = Counter(
    "tfb_idempotency_replayed_total",
    "Idempotent invocations served from a cached completed response (replay).",
)

idempotency_failopen_total = Counter(
    "tfb_idempotency_failopen_total",
    "Idempotency operations that FAILED OPEN because Valkey was unavailable or errored.",
    labelnames=("op",),
)
