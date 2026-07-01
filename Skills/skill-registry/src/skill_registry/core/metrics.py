"""Prometheus metrics for the skill registry."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

valkey_up = Gauge(
    "skill_registry_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when Valkey is unavailable (soft dependency).",
)

# ── WP03: verifier-side revocation mirror (shared scheme) ──────────────────────
revocation_check_skipped_total = Counter(
    "skill_registry_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable "
    "(fail-open: the token was ACCEPTED — revocation is defense-in-depth).",
)

revocation_rejected_total = Counter(
    "skill_registry_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation "
    "mirror, by the rule that matched.",
    labelnames=("rule",),
)

# ── WP11: registration + version retention ─────────────────────────────────────
skill_registered_total = Counter(
    "skill_registry_skill_registered_total",
    "Skills/versions registered, by kind (skill | version).",
    labelnames=("kind",),
)

version_retired_total = Counter(
    "skill_registry_version_retired_total",
    "Skill versions retired because the active-version count exceeded the retention cap.",
)

# ── WP11: manifest health poll state machine ───────────────────────────────────
health_transitions_total = Counter(
    "skill_registry_health_transitions_total",
    "Skill health state transitions observed by the manifest poll, by destination state.",
    labelnames=("to_status",),
)

health_poll_total = Counter(
    "skill_registry_health_poll_total",
    "Manifest polls performed, by outcome (ok | unchanged | error).",
    labelnames=("outcome",),
)

skills_by_status = Gauge(
    "skill_registry_skills_by_status",
    "Number of skill-health rows in each status at the last poll sweep.",
    labelnames=("status",),
)
