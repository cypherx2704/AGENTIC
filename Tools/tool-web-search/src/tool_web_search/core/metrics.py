"""Prometheus metrics for the tool-web-search MCP server."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Invoke ──────────────────────────────────────────────────────────────────────
invoke_total = Counter(
    "tws_invoke_total",
    "Total tool invocations handled.",
    labelnames=("provider", "status"),
)

invoke_duration_seconds = Histogram(
    "tws_invoke_duration_seconds",
    "End-to-end /mcp/v1/invoke duration in seconds.",
    labelnames=("provider",),
)

invoke_rejected_total = Counter(
    "tws_invoke_rejected_total",
    "Invocations rejected before/after the provider call, by reason "
    "(scope_denied | schema_invalid | output_too_large | provider_error).",
    labelnames=("reason",),
)

manifest_served_total = Counter(
    "tws_manifest_served_total",
    "GET /manifest responses by outcome (200 = body returned, 304 = ETag matched).",
    labelnames=("status",),
)

# ── Valkey (soft dependency) ────────────────────────────────────────────────────
valkey_up = Gauge(
    "tws_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when Valkey is unavailable (soft dependency).",
)

# ── Token revocation (WP03 verifier-side mirror) ─────────────────────────────────
revocation_check_skipped_total = Counter(
    "tws_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable "
    "(fail-open: the token was ACCEPTED — revocation is defense-in-depth).",
)

revocation_rejected_total = Counter(
    "tws_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation "
    "mirror, by the rule that matched.",
    labelnames=("rule",),
)

# ── Per-tenant rate limiting (fail-open) ─────────────────────────────────────────
rate_limit_rejected_total = Counter(
    "tws_rate_limit_rejected_total",
    "Requests rejected (429 RATE_LIMIT_EXCEEDED) by the pre-request fixed-window "
    "rate-limit check.",
    labelnames=("dimension",),
)

rate_limit_failopen_total = Counter(
    "tws_rate_limit_failopen_total",
    "Rate-limit checks that FAILED OPEN (allowed) because Valkey was unavailable or "
    "errored — availability wins over enforcement.",
    labelnames=("op",),
)

# ── Idempotency (Contract-9 style) ──────────────────────────────────────────────
idempotency_replayed_total = Counter(
    "tws_idempotency_replayed_total",
    "Idempotent invocations served from a cached completed response (replay).",
)

idempotency_failopen_total = Counter(
    "tws_idempotency_failopen_total",
    "Idempotency operations that FAILED OPEN (proceeded without the guarantee) "
    "because Valkey was unavailable or errored.",
    labelnames=("op",),
)
