"""Prometheus metrics for the Memory service."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

requests_total = Counter(
    "memory_requests_total",
    "Memory operations handled, by operation + status.",
    labelnames=("operation", "status"),
)

request_duration_seconds = Histogram(
    "memory_request_duration_seconds",
    "End-to-end memory operation duration in seconds.",
    labelnames=("operation",),
)

valkey_up = Gauge(
    "memory_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when unavailable (soft dependency).",
)

# ── Embeddings (llms-gateway client + mock fallback) ─────────────────────────────
embed_calls_total = Counter(
    "memory_embed_calls_total",
    "Embedding generations, by source: 'gateway' (llms-gateway), 'mock' (deterministic "
    "offline fallback — forced or fail-open when the gateway was unreachable).",
    labelnames=("source",),
)

embed_failopen_total = Counter(
    "memory_embed_failopen_total",
    "Embedding calls that FELL BACK to the deterministic mock embedder because the "
    "llms-gateway was unreachable or errored.",
)

# ── Store path ───────────────────────────────────────────────────────────────────
dedup_bumped_total = Counter(
    "memory_dedup_bumped_total",
    "Stores that hit a >= threshold near-duplicate and BUMPED the existing memory "
    "instead of inserting a near-copy.",
)

store_billing_write_failed_total = Counter(
    "memory_store_billing_write_failed_total",
    "Usage/outbox write failures on the store path (best-effort; the response is "
    "unaffected).",
    labelnames=("reason",),
)

# ── Retrieval scoring (Generative Agents composite re-rank) ──────────────────────
scoring_reranked_total = Counter(
    "memory_scoring_reranked_total",
    "Search responses re-ranked by the composite score (MEMORY_SCORING_ENABLED on); "
    "0 means every search used the default pure-cosine order.",
)

# ── Contradiction / temporal validity ────────────────────────────────────────────
memory_superseded_total = Counter(
    "memory_superseded_total",
    "Prior memories marked superseded (valid_until + superseded_by_id set) by a "
    "conflicting newer store (MEMORY_CONTRADICTION_ENABLED on).",
)

# ── Consolidation / forgetting (opt-in background routine) ────────────────────────
consolidation_forgotten_total = Counter(
    "memory_consolidation_forgotten_total",
    "Low-importance, old memories soft-deleted to the audit trail by the consolidation "
    "routine (MEMORY_CONSOLIDATION_ENABLED on).",
)

# ── Revocation mirror (WP03) ─────────────────────────────────────────────────────
revocation_check_skipped_total = Counter(
    "memory_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable "
    "(fail-open: the token was ACCEPTED — revocation is defense-in-depth).",
)

revocation_rejected_total = Counter(
    "memory_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation mirror, "
    "by the rule that matched.",
    labelnames=("rule",),
)

# ── Quota / rate limiting (Contract-19 memory limits) ────────────────────────────
quota_rejected_total = Counter(
    "memory_quota_rejected_total",
    "Requests rejected (429) by a quota/rate check, by the dimension that tripped "
    "(memories_max | storage_bytes_max | stores_per_min | retrieves_per_min).",
    labelnames=("dimension",),
)

quota_failopen_total = Counter(
    "memory_quota_failopen_total",
    "Quota/rate checks that FAILED OPEN (allowed) because limits were unresolved or "
    "Valkey/DB was unavailable — availability wins over enforcement.",
    labelnames=("op",),
)

# ── GDPR + TTL ───────────────────────────────────────────────────────────────────
gdpr_wiped_total = Counter(
    "memory_gdpr_wiped_total",
    "Principals wiped via POST /v1/gdpr/wipe (one increment per wipe request).",
)

ttl_swept_total = Counter(
    "memory_ttl_swept_total",
    "Expired memories hard-deleted by the background TTL sweep.",
)
