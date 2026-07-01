"""Prometheus metrics for the tool registry."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

valkey_up = Gauge(
    "tool_registry_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when Valkey is unavailable (soft dependency).",
)

# ── WP03: verifier-side revocation mirror (shared scheme) ──────────────────────
revocation_check_skipped_total = Counter(
    "tool_registry_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable "
    "(fail-open: the token was ACCEPTED — revocation is defense-in-depth).",
)

revocation_rejected_total = Counter(
    "tool_registry_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation "
    "mirror, by the rule that matched.",
    labelnames=("rule",),
)

# ── WP11: registration + version retention ─────────────────────────────────────
tool_registered_total = Counter(
    "tool_registry_tool_registered_total",
    "Tools/versions registered, by kind (tool | version).",
    labelnames=("kind",),
)

version_retired_total = Counter(
    "tool_registry_version_retired_total",
    "Tool versions retired because the active-version count exceeded the retention cap.",
)

# ── WP11: manifest health poll state machine ───────────────────────────────────
health_transitions_total = Counter(
    "tool_registry_health_transitions_total",
    "Tool health state transitions observed by the manifest poll, by destination state.",
    labelnames=("to_status",),
)

health_poll_total = Counter(
    "tool_registry_health_poll_total",
    "Manifest polls performed, by outcome (ok | unchanged | error).",
    labelnames=("outcome",),
)

tools_by_status = Gauge(
    "tool_registry_tools_by_status",
    "Number of tool-health rows in each status at the last poll sweep.",
    labelnames=("status",),
)
