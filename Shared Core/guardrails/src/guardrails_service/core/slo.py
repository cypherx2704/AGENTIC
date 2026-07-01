"""SLO definitions + Prometheus alert-rule thresholds for the guardrails hot path (WP07 Ops).

This module is documentation-as-code: the histogram buckets live with the metric in
``core/metrics.py`` (``_CHECK_LATENCY_BUCKETS``); the alert thresholds below are the
operational contract those buckets serve. Nothing here runs at request time — it is a
single source of truth so the K8s/Prometheus rule files can be generated/kept in sync.

SLOs (first cycle)
------------------
* **Latency** — p99 ``guardrails_check_duration_seconds`` (per direction) < 100ms; p999
  < 250ms. The regex/heuristic rules are single-digit ms; the classifier rules budget
  50ms each. Buckets are dense below 100ms so the p99/p999 estimates are accurate.
* **Availability** — < 0.1% of checks 5xx (checks themselves always 200; 5xx means an
  infrastructure fault). Auth 401/403 and 429 rate-limits are NOT availability errors.
* **Audit completeness** — ``guardrails_persist_queue_dropped_total`` is the metering/audit
  loss signal; sustained drops mean the queue is undersized or the DB is too slow.

Prometheus alert-rule thresholds (PromQL sketches — wire these into the rules file)
-----------------------------------------------------------------------------------
* GuardrailsCheckLatencyHigh (warning):
    histogram_quantile(0.99,
      sum(rate(guardrails_check_duration_seconds_bucket[5m])) by (le, direction)) > 0.1
    for: 10m
* GuardrailsCheckLatencyCritical (critical):
    histogram_quantile(0.999,
      sum(rate(guardrails_check_duration_seconds_bucket[5m])) by (le)) > 0.25
    for: 5m
* GuardrailsPersistQueueBacklog (warning):
    guardrails_persist_queue_backlog > 1000 for: 5m
* GuardrailsPersistQueueDropping (critical):
    rate(guardrails_persist_queue_dropped_total[5m]) > 0 for: 5m
* GuardrailsViolationWriteFailing (warning):
    rate(guardrails_violation_write_failed_total[5m]) > 0 for: 10m
* GuardrailsRateLimitFailClosedSpike (warning) — limiter rejecting due to backend errors,
  not real overuse:
    rate(guardrails_rate_limit_decisions_total{outcome="limited",dimension="none"}[5m]) > 0
    for: 5m
* GuardrailsValkeyDown (info — soft dep):
    guardrails_valkey_up == 0 for: 15m
* GuardrailsRulesRegistryMismatch (critical — code/DB rule drift fails readiness):
    up{job="guardrails"} == 1 and guardrails_readyz_rules_registry == 0   # if exported
"""

from __future__ import annotations

# p-quantile latency objectives (seconds). Imported by tests / rule generators if needed.
CHECK_LATENCY_P99_SECONDS = 0.1
CHECK_LATENCY_P999_SECONDS = 0.25
PERSIST_QUEUE_BACKLOG_WARN = 1000
