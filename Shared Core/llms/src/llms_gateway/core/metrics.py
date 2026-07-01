"""Prometheus metrics for the gateway."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

requests_total = Counter(
    "llms_requests_total",
    "Total chat-completion requests handled.",
    labelnames=("provider", "model", "status"),
)

request_duration_seconds = Histogram(
    "llms_request_duration_seconds",
    "End-to-end chat-completion duration in seconds.",
    labelnames=("provider", "model"),
)

billing_write_failed_total = Counter(
    "llms_billing_write_failed_total",
    "Usage/outbox write failures after a provider already returned tokens.",
    labelnames=("reason",),
)

tokens_total = Counter(
    "llms_tokens_total",
    "Total tokens accounted, by direction.",
    labelnames=("provider", "model", "direction"),
)

valkey_up = Gauge(
    "llms_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when Valkey is unavailable (soft dependency).",
)

config_source = Gauge(
    "llms_config_source",
    "Active source of the alias/pricing/capability registries (1 = active): "
    "'db' once all registries loaded from Postgres, 'fallback' while on the "
    "in-code cold-start fallback maps.",
    labelnames=("source",),
)

revocation_check_skipped_total = Counter(
    "llms_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable "
    "(fail-open: the token was ACCEPTED — revocation is defense-in-depth).",
)

revocation_rejected_total = Counter(
    "llms_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation "
    "mirror, by the rule that matched.",
    labelnames=("rule",),
)

# ── WP05: rate limiting ────────────────────────────────────────────────────────
rate_limit_rejected_total = Counter(
    "llms_rate_limit_rejected_total",
    "Requests rejected (429 RATE_LIMIT_EXCEEDED) by the pre-request rate-limit "
    "check, by the dimension that tripped (requests | prompt_tokens | completion_tokens).",
    labelnames=("dimension",),
)

rate_limit_failopen_total = Counter(
    "llms_rate_limit_failopen_total",
    "Rate-limit checks/debits that FAILED OPEN (allowed) because Valkey was "
    "unavailable or errored — availability wins over enforcement.",
    labelnames=("op",),  # 'enforce_pre' | 'debit_tokens'
)

# ── WP05: idempotency (Contract-9) ──────────────────────────────────────────────
idempotency_replayed_total = Counter(
    "llms_idempotency_replayed_total",
    "Idempotent requests served from a cached completed response (replay).",
)

idempotency_conflict_total = Counter(
    "llms_idempotency_conflict_total",
    "Idempotent requests rejected (409) because a request with the same key was "
    "still in flight.",
)

idempotency_failopen_total = Counter(
    "llms_idempotency_failopen_total",
    "Idempotency operations that FAILED OPEN (proceeded without the guarantee) "
    "because Valkey was unavailable or errored.",
    labelnames=("op",),  # 'begin' | 'get_replay' | 'complete'
)

# ── WP05: plan / limits resolution (auth_client) ────────────────────────────────
plan_resolve_failopen_total = Counter(
    "llms_plan_resolve_failopen_total",
    "Plan/limits resolutions that FELL BACK to the default permissive tier because "
    "the plan could not be resolved from the JWT claim, cache, DB, or Auth.",
    labelnames=("reason",),  # 'no_claim' | 'unknown_plan' | 'db_error' | 'auth_error'
)

plan_cache_hits_total = Counter(
    "llms_plan_cache_hits_total",
    "Plan -> limits resolutions served from the in-process TTL cache.",
)

# ── WP05: chat-path core (streaming correctness + max_tokens + journal) ──────────
max_tokens_rejected_total = Counter(
    "llms_max_tokens_rejected_total",
    "Requests rejected (400 MAX_TOKENS_EXCEEDED) because the requested max_tokens "
    "exceeded the model's hard output cap.",
)

param_clamped_total = Counter(
    "llms_param_clamped_total",
    "Request parameters silently clamped to a model/plan ceiling (response carries "
    "X-Cypherx-Param-Clamped), by the clamped parameter.",
    labelnames=("param",),  # e.g. 'max_tokens'
)

stream_terminated_total = Counter(
    "llms_stream_terminated_total",
    "Streamed completions that ended on a non-normal terminal condition, by reason "
    "(provider_error | timeout | client_disconnect).",
    labelnames=("reason",),
)

billing_journal_appended_total = Counter(
    "llms_billing_journal_appended_total",
    "UsageWrite records appended to the local billing-replay journal after a DB "
    "usage write failed (best-effort durability for a later replay worker).",
)

billing_journal_failed_total = Counter(
    "llms_billing_journal_failed_total",
    "Billing-journal append failures (the journal write itself failed — fail-open, "
    "the tokens are lost from the replay path but the response is unaffected).",
)

billing_journal_replayed_total = Counter(
    "llms_billing_journal_replayed_total",
    "Journalled UsageWrite records successfully re-driven into the DB by replay_pending, "
    "by outcome (replayed | failed).",
    labelnames=("outcome",),
)

# ── WP06: per-key ACLs (Contract-18) ─────────────────────────────────────────────
acl_denied_total = Counter(
    "llms_acl_denied_total",
    "Requests rejected (403 FORBIDDEN, reason ACL_DENIED) by the per-key ACL check, "
    "by the dimension that was not permitted (model | provider | operation).",
    labelnames=("dimension",),
)

acl_failopen_total = Counter(
    "llms_acl_failopen_total",
    "Per-key ACL checks that FAILED OPEN (allowed) because no DB pool was wired or the "
    "ACL load errored — availability/the unrestricted default wins over enforcement.",
    labelnames=("reason",),  # 'no_pool' | 'db_error'
)

# ── WP06: multimodal / body caps ─────────────────────────────────────────────────
payload_too_large_total = Counter(
    "llms_payload_too_large_total",
    "Requests rejected (413 PAYLOAD_TOO_LARGE) by a multimodal/body cap, by the cap "
    "that tripped (body_bytes | image_count | image_bytes).",
    labelnames=("cap",),
)

# ── WP06: pricing-staleness watchdog ─────────────────────────────────────────────
pricing_staleness_seconds = Gauge(
    "llms_pricing_staleness_seconds",
    "Age in seconds of the most-recently-updated provider_pricing row at the last "
    "staleness check (-1 when the age could not be determined).",
)

# ── WP06: BYOK (bring-your-own-key) ──────────────────────────────────────────────
byok_key_source_total = Counter(
    "llms_byok_key_source_total",
    "Provider calls by which API key was used: 'tenant' when a tenant BYOK key was "
    "selected, 'platform' when the platform key was used (no/disabled BYOK or a "
    "fail-open fallback).",
    labelnames=("source", "provider"),
)
