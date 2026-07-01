"""Prometheus metrics for the RAG service."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Query ──────────────────────────────────────────────────────────────────────
query_total = Counter(
    "rag_query_total",
    "Total KB query requests handled, by outcome.",
    labelnames=("outcome",),  # ok | forbidden | not_found
)

query_duration_seconds = Histogram(
    "rag_query_duration_seconds",
    "End-to-end query duration in seconds.",
)

chunks_returned = Histogram(
    "rag_chunks_returned",
    "Number of chunks returned per query.",
    buckets=(0, 1, 3, 5, 10, 20, 50, 100),
)

query_search_mode_total = Counter(
    "rag_query_search_mode_total",
    "KB queries handled, by retrieval search_mode.",
    labelnames=("mode",),  # dense | hybrid | sparse
)

query_rerank_total = Counter(
    "rag_query_rerank_total",
    "KB queries that ran the optional rerank stage, by rerank source.",
    labelnames=("source",),  # llms | mock | fallback_base
)

# ── Ingest ─────────────────────────────────────────────────────────────────────
ingest_total = Counter(
    "rag_ingest_total",
    "Documents accepted for ingestion, by path.",
    labelnames=("path",),  # inline | finalize
)

ingest_dedup_total = Counter(
    "rag_ingest_dedup_total",
    "Finalize/ingest calls short-circuited by idempotency-replay or content dedup.",
    labelnames=("kind",),  # idempotency_replay | content_sha
)

chunks_indexed_total = Counter(
    "rag_chunks_indexed_total",
    "Total chunks embedded + stored across all documents.",
)

contextual_ingest_total = Counter(
    "rag_contextual_ingest_total",
    "Chunk-context generations during contextual ingest, by source.",
    labelnames=("source",),  # llms | mock | fallback_raw | disabled
)

# ── Worker ─────────────────────────────────────────────────────────────────────
worker_processed_total = Counter(
    "rag_worker_processed_total",
    "Ingestion worker messages processed, by outcome.",
    labelnames=("outcome",),  # completed | retried | dlq
)

# ── Embeddings ─────────────────────────────────────────────────────────────────
embeddings_total = Counter(
    "rag_embeddings_total",
    "Embedding batches produced, by source.",
    labelnames=("source",),  # llms | mock | fallback_mock
)

# ── KB ACL ─────────────────────────────────────────────────────────────────────
acl_denied_total = Counter(
    "rag_acl_denied_total",
    "Operations rejected (403 FORBIDDEN_KB) by the KB ACL check, by operation.",
    labelnames=("operation",),  # query | ingest | write | admin
)

# ── Quota ──────────────────────────────────────────────────────────────────────
quota_rejected_total = Counter(
    "rag_quota_rejected_total",
    "Requests rejected by a Contract-19 quota check, by the dimension that tripped.",
    labelnames=("dimension",),  # kbs_max | documents_per_kb_max | queries_per_min | storage_bytes_max
)

quota_failopen_total = Counter(
    "rag_quota_failopen_total",
    "Quota checks that FAILED OPEN (allowed) because the plan/limits or Valkey were "
    "unavailable — availability wins over enforcement.",
    labelnames=("reason",),  # no_plan | valkey_unavailable | no_pool
)

# ── Idempotency ────────────────────────────────────────────────────────────────
idempotency_replayed_total = Counter(
    "rag_idempotency_replayed_total",
    "Finalize requests served from a cached completed response (replay).",
)

idempotency_skipped_total = Counter(
    "rag_idempotency_skipped_total",
    "Idempotency operations that FAILED OPEN because Valkey was unavailable.",
    labelnames=("op",),  # begin | get_replay | complete
)

# ── Token revocation (WP03 mirror) ──────────────────────────────────────────────
revocation_check_skipped_total = Counter(
    "rag_revocation_check_skipped_total",
    "Inbound JWT revocation checks skipped because Valkey was unavailable (fail-open).",
)

revocation_rejected_total = Counter(
    "rag_revocation_rejected_total",
    "Inbound JWTs rejected (401 TOKEN_REVOKED) by the verifier-side revocation mirror.",
    labelnames=("rule",),
)

# ── Bootstrap / readiness ───────────────────────────────────────────────────────
bootstrap_running = Gauge(
    "rag_bootstrap_running",
    "1 when the platform-skills bootstrap loop is running (readiness gate per Component 10).",
)

bootstrap_completed = Gauge(
    "rag_bootstrap_completed",
    "1 once the platform-skills KB + default ACL row have been ensured at least once.",
)

valkey_up = Gauge(
    "rag_valkey_up",
    "1 when the last Valkey ping succeeded, 0 when Valkey is unavailable (soft dependency).",
)

# ── S3 deletion sweeper ─────────────────────────────────────────────────────────
s3_deletions_pending = Gauge(
    "rag_s3_deletions_pending",
    "Number of rows pending in rag.s3_deletions at the last sweep.",
)
