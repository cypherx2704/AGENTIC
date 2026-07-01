"""Prometheus metrics for the agent-runtime."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

tasks_total = Counter(
    "xagent_tasks_total",
    "Total agent tasks executed, by terminal status.",
    labelnames=("status",),
)

task_duration_seconds = Histogram(
    "xagent_task_duration_seconds",
    "End-to-end task execution duration in seconds.",
    labelnames=("status",),
)

stage_duration_seconds = Histogram(
    "xagent_stage_duration_seconds",
    "Per-pipeline-stage execution duration in seconds.",
    labelnames=("stage", "status"),
)

downstream_calls_total = Counter(
    "xagent_downstream_calls_total",
    "Outbound calls to downstream services, by target + outcome.",
    labelnames=("service", "outcome"),
)

# ── WP12 downstream clients (RAG / Memory / Registry / MCP) ───────────────────────
registry_manifest_cache_total = Counter(
    "xagent_registry_manifest_cache_total",
    "Tool-registry manifest cache lookups by outcome.",
    # hit: served fresh from cache; miss: not cached -> full GET + backfill;
    # revalidated: TTL expired -> If-None-Match -> 304 reuse (TTL reset);
    # refreshed: If-None-Match -> 200 new body; stale: transport/5xx -> served an
    # expired cached entry (fail-soft).
    labelnames=("outcome",),
)

mcp_circuit_breaker_state = Gauge(
    "xagent_mcp_circuit_breaker_open",
    "1 while an MCP (endpoint, agent) circuit breaker is OPEN, 0 when closed/half-open.",
    labelnames=("endpoint",),
)

mcp_invocations_total = Counter(
    "xagent_mcp_invocations_total",
    "MCP tool invocations by outcome.",
    # ok: 2xx result; rejected: terminal 4xx (no retry); error: transport/5xx after
    # retries exhausted; circuit_open: fast-failed because the breaker was open.
    labelnames=("outcome",),
)

event_write_failed_total = Counter(
    "xagent_event_write_failed_total",
    "Task/outbox event write failures (task row could not be finalised).",
    labelnames=("reason",),
)

valkey_up = Gauge(
    "xagent_valkey_up",
    "1 if the last Valkey ping succeeded, 0 otherwise (SOFT dependency — never gates readiness).",
)

llm_finish_reason_unknown_total = Counter(
    "xagent_llm_finish_reason_unknown_total",
    "LLM completions whose finish_reason was outside the known gateway enum (treated as 'stop').",
)

# ── Agent-config Valkey read-through cache (Component 1, WP08) ─────────────────────
agent_config_cache_total = Counter(
    "xagent_agent_config_cache_total",
    "Agent-config cache lookups by outcome.",
    # hit: served from Valkey; miss: not cached -> DB read + backfill; bypass: cache
    # disabled / no Valkey configured (straight DB read); error: Valkey errored (FAIL
    # OPEN to a DB read); invalidate: a PUT busted the key.
    labelnames=("outcome",),
)

# ── OpenTelemetry span export (Contract 8, WP08) ───────────────────────────────────
otel_tracing_enabled = Gauge(
    "xagent_otel_tracing_enabled",
    "1 if the OTLP span exporter was wired at startup (endpoint set + SDK present), else 0.",
)

# ── Live token revocation — verifier-side mirror (Component 3c, WP03) ─────────────
revocation_checks_total = Counter(
    "xagent_revocation_checks_total",
    "Inbound-JWT revocation checks, by outcome.",
    # clean: passed; revoked: rejected (jti/kid/agent hit); skipped: Valkey unavailable
    # (FAIL-OPEN — token accepted); disabled: the check flag was off.
    labelnames=("outcome",),
)

revocation_check_skipped_total = Counter(
    "xagent_revocation_check_skipped_total",
    "Revocation checks that FAILED OPEN because Valkey was unavailable (token accepted).",
)

# ── Task lifecycle reliability (cancel / timeout / idempotency / authorize, WP08) ──
task_cancels_total = Counter(
    "xagent_task_cancels_total",
    "Cooperative DELETE /v1/tasks/{id} cancel requests, by outcome.",
    # accepted: cancel signal set on a running task (202); conflict: task already terminal
    # (409); not_found: unknown / cross-tenant (404); unavailable: cancel store down (503).
    labelnames=("outcome",),
)

task_timeouts_total = Counter(
    "xagent_task_timeouts_total",
    "Tasks terminated by the per-task asyncio.timeout guard (in-process).",
)

idempotency_requests_total = Counter(
    "xagent_idempotency_requests_total",
    "POST /v1/tasks requests carrying an Idempotency-Key, by outcome.",
    # new: first sighting (reservation taken); replay: completed record replayed;
    # conflict: duplicate still in_flight (409); unavailable: configured Valkey errored
    # FAIL-CLOSED (503); disabled: no Valkey configured / feature off (allow-through).
    labelnames=("outcome",),
)

authorize_checks_total = Counter(
    "xagent_authorize_checks_total",
    "Auth layer-B task:execute authorize checks, by outcome.",
    # allow: authorized (fresh or cached); deny: Auth returned deny (403);
    # cache_hit: served from the Valkey verdict cache; fail_open: Auth/Valkey error
    # accepted (availability wins); disabled: the check flag was off.
    labelnames=("outcome",),
)

sweeper_runs_total = Counter(
    "xagent_sweeper_runs_total",
    "Backup-sweeper wake cycles, by outcome (ok | error — the loop never dies).",
    labelnames=("outcome",),
)

sweeper_tasks_swept_total = Counter(
    "xagent_sweeper_tasks_swept_total",
    "Tasks the backup sweeper finalised as failed (stuck past their deadline).",
)

sweeper_rows_deleted_total = Counter(
    "xagent_sweeper_rows_deleted_total",
    "Rows the backup sweeper deleted during retention, by table.",
    labelnames=("table",),
)
