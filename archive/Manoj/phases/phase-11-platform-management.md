# Phase 11 — Platform Management
> **Status:** ⏳ Pending | **Depends On:** Phase 1–9 | **Blocks:** Phase 12
> **First Cycle:** 📋 Not required for first cycle. Begin after core services are operational.

---

## Phase Overview

Platform Management is the **control plane** for the entire CypherX AI platform. It does not run agents — it manages, monitors, and governs everything else. It aggregates observability, manages deployments, tracks costs, and publishes billing events to px0.

**Deliverable:** A platform management service with service registry, config management, cost roll-up, and centralised deployment tracking.

> 🏗️ **Service Architecture Note:** The internal architecture of the platform management service (config propagation mechanism, deployment orchestration, billing event aggregation pipeline) must be planned separately before implementation begins.

---

## High Level Design

### System Context

```
                     ┌────────────────────────────────────────────┐
                     │         PLATFORM MANAGEMENT                 │
                     │                                             │
  Admin / DevOps ───►│  Service Registry   (who's running, status) │
  Billing ──────────►│  Cost Roll-up       (per tenant, monthly)   │
  CI/CD ────────────►│  Deployment Manager (track what's deployed)  │
  Alerts ───────────►│  Alert Router       (→ PagerDuty / Slack)   │
                     │  Config Store       (versioned, hot-reload)  │
                     └──────────────┬──────────────────────────────┘
                                    │
           ┌────────────────────────┼────────────────────────────┐
           ▼                        ▼                             ▼
    Kafka consumers           PostgreSQL                    px0 Billing
    (usage events →          (platform schema)             (billing events
     cost aggregation)                                      via REST/Kafka)
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> All items are 📋 — none required for first cycle.

---

### Component 1 — Service Registry 📋

**What it is:** Catalogue of every running service with health status and metadata.

> **Derived/cached view, never authoritative.** K8s API owns liveness (via EndpointSlices); ArgoCD owns "deployed version". This table is a denormalised cache for the platform-mgmt UI and Grafana queries. Mutations come from the sync jobs below, NOT from external API writes. `depends_on` is a free-form display field — the *enforceable* dependency graph lives in `auth.service_acl` (Phase 2 / 7 / 8 / 11 seed migrations).

**PostgreSQL (`platform.services`):**
```sql
CREATE TABLE platform.services (
  service_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  name           VARCHAR(100) NOT NULL UNIQUE,
  display_name   VARCHAR(255),
  namespace      VARCHAR(100) NOT NULL,
  version        VARCHAR(50)  NOT NULL,             -- from ArgoCD app metadata
  status         VARCHAR(20)  NOT NULL DEFAULT 'healthy',
                 -- healthy | degraded | offline | deploying
  health_url     VARCHAR(500),                       -- e.g. http://<svc>.<ns>:8080/livez
  metrics_url    VARCHAR(500),
  depends_on     TEXT[]       NOT NULL DEFAULT '{}', -- display-only; authoritative source is auth.service_acl
  last_health_at TIMESTAMPTZ,
  metadata       JSONB        NOT NULL DEFAULT '{}'
);
-- Platform-scoped table (no tenant_id, no RLS). Mutations gated by platform:admin
-- OR by the internal sync jobs (which run with the platform-admin service identity).
```

**Sync jobs (two cadences, two sources):**
```
Health poll (every 30s):
  For each row in platform.services:
    GET {health_url}  -- uses /livez per Contract 7
  → Update last_health_at + status (healthy | degraded | offline)
  → State-change → emit alert event (Component 5 path)
  Status changes propagate within 30s (was 60s — too slow for production paging).

Full reconcile from K8s (every 5 min):
  - List Services + Endpoints in shared-core, xagent, tools, platform-mgmt, ingress namespaces.
  - Upsert platform.services rows with current namespace, health/metrics URLs.
  - Mark rows whose backing Service no longer exists as status='offline'.
  - New services discovered automatically without registry API calls.

Version reconcile from ArgoCD (every 5 min):
  - GET ArgoCD app list; for each app, extract its image tag.
  - Upsert platform.services.version with the resolved tag.
  - Status during sync = 'deploying'.
```

---

### Component 2 — Config Management 📋

**What it is:** Centralised store for non-sensitive service configuration (feature flags, tuning params).

> **Append-only versioning** (prior draft mutated rows in place with `UNIQUE (service, environment, key)` — that destroyed history on every PUT and made `/history` a lie).

**PostgreSQL (`platform.config`):**
```sql
CREATE TABLE platform.config (
  config_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  service        VARCHAR(100) NOT NULL,           -- service name or "global"
  environment    VARCHAR(20)  NOT NULL,           -- dev | staging | prod
  key            VARCHAR(255) NOT NULL,
  value          TEXT         NOT NULL,
  version        INTEGER      NOT NULL,           -- monotonic per (service, environment, key)
  description    TEXT,
  effective_from TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_by     UUID,

  UNIQUE (service, environment, key, version)
);

CREATE INDEX idx_config_lookup
  ON platform.config(service, environment, key, version DESC);
-- Platform-scoped table (no tenant_id, no RLS). Mutations gated by platform:admin
-- OR fine-grained config:write:<service> scope.

-- Latest-version view for the common-case read:
CREATE VIEW platform.config_current AS
SELECT DISTINCT ON (service, environment, key)
       config_id, service, environment, key, value, version,
       description, effective_from, updated_at, updated_by
FROM   platform.config
ORDER BY service, environment, key, version DESC;
```

**Config API:**
```
GET    /v1/config/{service}                        Get all current config for a service
GET    /v1/config/{service}/{key}                  Get current value
GET    /v1/config/{service}/{key}?at=<RFC3339>     Point-in-time (uses effective_from)
PUT    /v1/config/{service}/{key}                  Insert new version (does NOT mutate prior)
                                                   Body: { "value": "...", "description": "..." }
GET    /v1/config/{service}/{key}/history          Full version history (with version + effective_from)

Scope gating:
  Read  (GET):  platform:read OR config:read:<service>
  Write (PUT):  platform:admin OR config:write:<service>
  All writes audit-logged to auth.audit_log with old + new values.
```

**Hot-reload via outbox (REQUIRED — DB write + Kafka event must not diverge):**

```sql
-- platform.outbox table (reused for any platform-mgmt events; see Component 6 too):
CREATE TABLE platform.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,        -- service name (so per-service hot-reload is ordered)
  payload       JSONB        NOT NULL,        -- Contract 5 envelope
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_outbox_unpublished
  ON platform.outbox(created_at) WHERE published_at IS NULL;
```

Write path (one transaction):
```
BEGIN;
  INSERT INTO platform.config (...) VALUES (..., next_version, NOW(), ...);
  INSERT INTO platform.outbox (topic, partition_key, payload) VALUES
    ('cypherx.platform.config.updated', $service, <Contract 5 envelope>);
COMMIT;
```

Publisher loop: standard pattern (batch SELECT 100, publish with partition_key, mark `published_at`, backoff, DLQ after 10 attempts).

Services that support hot-reload subscribe to `cypherx.platform.config.updated` with per-pod consumer groups (same broadcast pattern as Phase 10 cancellation — every pod needs the update).

> **Topic provisioning:** Phase 11 ships Terraform that provisions `cypherx.platform.config.updated` (`partitions: 3, replication: 3, cleanup.policy: delete, retention: 7d`) and its `.dlq` pair. Auto-creation forbidden (Phase 1 post-edit).

---

### Component 3 — Cost Roll-up & Billing 📋

**What it is:** Aggregates LLM token costs (from Kafka) per tenant per month. Publishes billing events to px0.

**Producer / consumer topology (clarifying who emits what):**
```
Producers (NOT platform-mgmt):
  cypherx.llms.request.completed             ← Phase 3 LLMs gateway (per-request LLM cost)

Producers (platform-mgmt internal jobs):
  cypherx.billing.usage.recorded             ← TWO new jobs in this phase:
    (a) k8s-compute-cost-emitter              — scrapes Prometheus for per-tenant CPU/mem,
                                                converts to $ via node-pool price table,
                                                emits per-tenant per-hour cost rows.
    (b) s3-storage-cost-emitter               — scrapes S3 bucket-level metrics + KMS
                                                request counts, converts to $, emits per
                                                tenant per-day cost rows.

Consumer (platform-mgmt):
  cypherx-billing-usage-aggregator           ← consumes BOTH topics above, writes to
                                                platform.tenant_costs.
                                              Does NOT consume its own output.

End-of-month push to px0: see "px0 billing integration" below.
```

**PostgreSQL (`platform.tenant_costs`):**
```sql
CREATE TABLE platform.tenant_costs (
  id               UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID          NOT NULL,
  period           DATE          NOT NULL,                  -- first day of month, e.g. '2026-05-01'
  llm_cost_usd     NUMERIC(12,4) NOT NULL DEFAULT 0,
  compute_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
  storage_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
  total_cost_usd   NUMERIC(12,4) NOT NULL                   -- generated column (Postgres 12+)
                   GENERATED ALWAYS AS
                   (llm_cost_usd + compute_cost_usd + storage_cost_usd) STORED,
  last_updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

  UNIQUE (tenant_id, period)
);

-- Tenant-scoped table — RLS required (Contract 13):
ALTER TABLE platform.tenant_costs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_costs_isolation ON platform.tenant_costs FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
-- Cross-tenant reads only via platform-mgmt's platform-admin DB role
-- (used by the billing aggregator + px0-push job + Grafana cost dashboard).
```

> `total_cost_usd` is a **generated column** — prior draft maintained it manually alongside the three component columns; forgetting to update it on any one path silently produced wrong totals. The generated column removes the entire bug class.

> `period` is `DATE` (first day of month) — easier range queries than the prior `VARCHAR(7)` "2026-05" format.

**Kafka consumer:** `cypherx-billing-usage-aggregator`
```
Consumes: cypherx.llms.request.completed     (LLM cost from Phase 3)
          cypherx.billing.usage.recorded     (compute/storage cost from platform-mgmt jobs)

For each event:
  period = first day of event.produced_at month
  INSERT INTO platform.tenant_costs (tenant_id, period, <cost-column>)
    VALUES ($t, $p, $cost)
    ON CONFLICT (tenant_id, period)
    DO UPDATE SET <cost-column> = platform.tenant_costs.<cost-column> + $cost,
                  last_updated_at = NOW();

At end of month: trigger px0 push (see below).
```

**Billing Emitter abstraction (NEW — pluggable, not px0-only):**

For "Externally Operable" the platform must support multiple billing backends — px0 (internal), Stripe (most external customers), Chargebee, ZuoraJ, or webhook-only for self-managed deployments. Hard-wiring to px0 makes standalone use impossible.

```python
class IBillingEmitter(Protocol):
    def push_invoice(tenant_id: UUID, period: date, line_items: list[LineItem]) -> EmitterResult: ...
    def acknowledge_callback(payload: dict) -> None: ...
    def health() -> bool: ...

# Concrete implementations:
class Px0BillingEmitter:          ...   # internal CypherX-managed cloud
class StripeBillingEmitter:       ...   # external customers using Stripe
class ChargebeeBillingEmitter:    ...   # external customers using Chargebee
class WebhookBillingEmitter:      ...   # generic — POST to customer-configured URL (Contract 21 signing)
class ManualInvoiceEmitter:       ...   # enterprise — generate PDF, drop in S3, email; no API push
```

**Selection per tenant:** `auth.tenants.source_metadata.billing_emitter` (one of: `px0 | stripe | chargebee | webhook | manual-invoice`). Configured at tenant creation; mutable by `platform:admin` for migrations.

**Loader:** at platform-mgmt service startup, `BillingEmitterRegistry` resolves the implementation by name per-tenant. The push job (below) calls `registry.for(tenant).push_invoice(...)`. Adding a new backend = adding one class + one registry entry; no changes to the push pipeline.

**Failure handling:** each emitter MUST be idempotent on `(tenant_id, period)` and SHOULD return acknowledged-by-backend status. The retry policy and `platform.billing_push_log` table are SHARED across emitters — the abstraction is per-tenant, the persistence is uniform.

---

**px0 billing integration (with retry + idempotency + audit) — `Px0BillingEmitter` implementation:**

```sql
-- Track every push attempt — required for failure recovery and audit.
CREATE TABLE platform.billing_push_log (
  id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID          NOT NULL,
  period            DATE          NOT NULL,
  amount_usd        NUMERIC(12,4) NOT NULL,
  idempotency_key   VARCHAR(100)  NOT NULL,                -- "billing-<tenant>-<period>"
  status            VARCHAR(20)   NOT NULL DEFAULT 'pending',
                    -- pending | sent | failed | acknowledged
  attempts          INTEGER       NOT NULL DEFAULT 0,
  last_attempted_at TIMESTAMPTZ,
  last_error        TEXT,
  response_payload  JSONB,
  created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  acknowledged_at   TIMESTAMPTZ
);
CREATE INDEX idx_billing_push_pending
  ON platform.billing_push_log(period) WHERE status IN ('pending','failed');
```

```
POST https://api.px0.cypherx.ai/v1/billing/events
Headers:
  Authorization:   Bearer <PX0_API_KEY>
  Idempotency-Key: billing-<tenant_id>-<period>      ← px0 dedupes; safe to retry
Body: {
  "tenant_id": "<uuid>",
  "period":    "2026-05-01",
  "line_items": [
    { "type": "llm_tokens",  "amount_usd": 42.50, "units": 5000000, "unit": "tokens" },
    { "type": "compute",     "amount_usd": 8.20 },
    { "type": "storage",     "amount_usd": 1.05 }
  ]
}

Retry policy:
  - Exponential backoff: 30s, 2 min, 10 min, 1 hr, 6 hr (max 24h total).
  - Each attempt updates platform.billing_push_log (attempts++, last_error).
  - After 24h: status='failed'; PagerDuty alert (severity: critical) — billing data
    will be lost without operator intervention.
  - On 200/202 response: status='sent' (acknowledged by px0); store response.
  - On callback from px0 confirming ingestion: status='acknowledged'.

Idempotency at px0: same Idempotency-Key returned for the same (tenant, period)
prevents double-billing on retry. Required, not optional.
```

---

### Component 4 — Deployment Tracker 📋

**What it is:** Records what version of each service is deployed in each environment.

> **Provenance:** entries are written by **ArgoCD notifications webhook** (preferred — single source, no CI side-change), not by GitHub Actions directly. ArgoCD already knows the truth (synced revision, app health); we just persist it for UI/audit. If the webhook is unavailable, the Component 1 ArgoCD sync job (every 5 min) is the backup writer.

**PostgreSQL (`platform.deployments`):**
```sql
CREATE TABLE platform.deployments (
  deployment_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  service        VARCHAR(100) NOT NULL,
  environment    VARCHAR(20)  NOT NULL,
  version        VARCHAR(50)  NOT NULL,
  image_tag      VARCHAR(255) NOT NULL,
  deployed_by    VARCHAR(255),                   -- "argocd:<sync-id>" or "ci:<actions-run-id>"
  source_commit  VARCHAR(40),                    -- gitops repo SHA
  deployed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  status         VARCHAR(20)  NOT NULL DEFAULT 'success',
                 -- success | failed | rolled_back
  rollback_to    UUID                             -- previous deployment_id (NULL if not a rollback)
);

CREATE INDEX idx_deployments_service ON platform.deployments(service, environment, deployed_at DESC);
-- Platform-scoped table; mutations gated by platform:admin OR by the deployment-tracker
-- service identity used by the ArgoCD webhook receiver.
```

**API:**
```
GET  /v1/deployments/{service}           Current deployment per env
                                         Scope: platform:read OR config:read:<service>
GET  /v1/deployments/{service}/history   Deployment history (paginated)
                                         Scope: same as above
POST /v1/deployments/{service}/rollback  Trigger rollback
                                         Scope: platform:admin (NO fine-grained alternative —
                                                rollback is high blast radius)
                                         Rate limit: 1 per (service, environment) per 5 min
                                                (prevents flap loops via repeated rollback).

Rollback mechanism (concrete):
  1. Verify scope + rate limit.
  2. Identify the previous successful deployment row (status='success', deployed_at<current).
  3. Use the cypherx-gitops-bot GitHub App (Phase 1 Component 18 post-edit) to:
       a. Read the deployment file at envs/<env>/<service>/values.yaml in the gitops repo.
       b. Revert image.tag to the previous deployment's image_tag.
       c. Commit on a branch + open auto-merge PR (auto-merged in dev/staging;
          requires manual approval in prod — ArgoCD prod sync is manual anyway).
  4. INSERT a new platform.deployments row with rollback_to = <prior deployment_id>,
     status='success' (the rollback commit itself is the success; ArgoCD picks up next sync).
  5. Audit-log to auth.audit_log: { action: 'deployment.rollback', service, env,
                                    from_version, to_version, requested_by_agent_id }.

Webhook receiver (ArgoCD → platform-mgmt):
  POST /v1/internal/argocd-webhook
  Auth: shared HMAC secret (Doppler: alerting/argocd_webhook_hmac_key) — NOT a JWT,
        ArgoCD doesn't speak our service-JWT format.
  Verifies signature; inserts a deployments row on app.sync.succeeded / failed events.
```

---

### Component 5 — Alerting 📋

**What it is:** Routes Prometheus Alertmanager alerts to the right channel.

**Alertmanager routes (secrets from Doppler, paths explicit):**
```yaml
route:
  receiver: slack-platform
  routes:
    - match: { severity: critical }
      receiver: pagerduty
    - match: { service: xagent }
      receiver: slack-xagent
    - match: { type: billing }
      receiver: slack-billing

receivers:
  - name: pagerduty
    pagerduty_configs:
      - service_key: $PAGERDUTY_SERVICE_KEY            # Doppler: alerting/pagerduty/service_key
  - name: slack-platform
    slack_configs:
      - api_url: $SLACK_WEBHOOK_PLATFORM               # Doppler: alerting/slack/platform_webhook
        channel: "#platform-alerts"
  - name: slack-xagent
    slack_configs:
      - api_url: $SLACK_WEBHOOK_XAGENT                 # Doppler: alerting/slack/xagent_webhook
        channel: "#xagent-alerts"
  - name: slack-billing
    slack_configs:
      - api_url: $SLACK_WEBHOOK_BILLING                # Doppler: alerting/slack/billing_webhook
        channel: "#billing-alerts"
```

**Alert rules (defined in Prometheus — every rule MUST carry `runbook_url`; CI lint enforces):**
```yaml
groups:
  - name: cypherx-platform
    rules:
      - alert: ServiceDown
        expr: up{job=~"auth-service|llms-gateway|guardrails-service|xagent|.*"} == 0
        for: 2m
        labels: { severity: critical }
        annotations:
          summary:     "{{ $labels.job }} is down ({{ $labels.namespace }})"
          description: "Service {{ $labels.job }} has been down for >2m."
          runbook_url: "https://runbooks.cypherx.ai/service-down"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status_code=~"5.."}[5m]) > 0.05
        for: 5m
        labels: { severity: warning }
        annotations:
          summary:     "{{ $labels.job }} 5xx rate >5% for 5m"
          runbook_url: "https://runbooks.cypherx.ai/high-error-rate"

      - alert: LLMCostAnomaly
        # Per-environment thresholds (first-cycle approach; per-tenant z-score is 📋).
        # Threshold set via Alertmanager external_labels; here we show prod.
        expr: |
          (
            increase(llm_cost_usd_total[1h]) > 1000  and on()  cypherx_env == "prod"
          ) or (
            increase(llm_cost_usd_total[1h]) > 100   and on()  cypherx_env == "staging"
          ) or (
            increase(llm_cost_usd_total[1h]) > 10    and on()  cypherx_env == "dev"
          )
        for: 5m
        labels: { severity: warning, type: billing }
        annotations:
          summary:     "LLM cost anomaly: ${{ $value }} in last 1h ({{ $labels.cypherx_env }})"
          runbook_url: "https://runbooks.cypherx.ai/llm-cost-anomaly"

      - alert: KafkaConsumerLag
        expr: kafka_consumer_lag > 10000
        for: 5m
        labels: { severity: warning }
        annotations:
          summary:     "Kafka consumer {{ $labels.consumer_group }} lag >10k"
          runbook_url: "https://runbooks.cypherx.ai/kafka-consumer-lag"

      - alert: BillingPushFailed
        expr: max_over_time(platform_billing_push_attempts[1h]) >= 5
        for: 1h
        labels: { severity: critical, type: billing }
        annotations:
          summary:     "Billing push to px0 failing for {{ $labels.tenant_id }} {{ $labels.period }}"
          description: "5+ attempts in the last hour. Manual intervention required by 24h mark."
          runbook_url: "https://runbooks.cypherx.ai/billing-push-failed"
```

> **Per-tenant z-score anomaly detection** (rolling 7-day window) is 📋 — requires a recording rule or a small ML pipeline.

> **Alert lint:** CI rule `prometheus-alerts-lint.yml` rejects PRs that add alert rules without `summary` + `runbook_url` annotations. Operator-hostile alerts don't merge.

---

### Component 6 — px0 Tenant Bridge 📋 (NEW)

Phase 0 Contract 13 (post-edit) explicitly defers the px0 ↔ CypherX tenant bridge to Phase 11; Phase 1's ECR list includes `cypherx/px0-bridge`. The earlier draft of Phase 11 omitted the component entirely. Without it, tenants stay manually-seeded forever and Contract 13's "Phase 11 owns the bridge" promise is empty.

**What it is:** A dedicated service that consumes px0 organisation-lifecycle events from Kafka and reflects them into CypherX as tenant create/suspend/delete operations.

**Deployment:**
```
Namespace:   px0-bridge
Service:     px0-bridge
Replicas:    min 2, max 4 (HPA on Kafka consumer lag)
Image:       cypherx/px0-bridge  (Phase 1 ECR list)
```

**Kafka topics consumed (foreign-prefix per Phase 0 Contract 5 allow-list):**
```
px0.org.created       → POST /v1/admin/tenants on Auth (Phase 2 Component 1b)
                        Body { tenant_id: <px0.org_id>, name, plan, source: 'px0-bridge' }
                        ON CONFLICT DO NOTHING (idempotent for at-least-once delivery)

px0.org.suspended     → PATCH /v1/admin/tenants/{id}/suspend on Auth
                        Idempotent (suspend is a state, not a delta).

px0.org.deleted       → DELETE /v1/admin/tenants/{id} on Auth (soft-delete)
                        Then fan-out: emit cypherx.tenant.wipe.requested to trigger
                        per-service GDPR sweep (Memory Phase 6 already implements wipe;
                        other services subscribe and run their own DELETE pass).
```

**Topic provisioning:**
- `px0.*` topics are owned by px0; CypherX does NOT create them. px0-bridge consumes from the existing px0 cluster (or the same MSK cluster if px0 is co-located — Phase 1 RBAC permits cross-prefix read for the px0-bridge service identity only).
- `cypherx.tenant.wipe.requested` IS owned by CypherX; Phase 11 ships Terraform that provisions it (`partitions: 6, replication: 3, cleanup.policy: delete, retention: 30d`) + DLQ pair.

**Outbox + audit:**
- px0-bridge uses `platform.outbox` for `cypherx.tenant.wipe.requested` (transactional with bridge's own state table — `platform.px0_org_log` records every received event with status `received | applied | failed`).
- Auth-side `auth.audit_log` (Phase 2 Component 6) records every tenant mutation; px0-bridge's caller identity (`source: 'px0-bridge'`) is attached.

**Service ACL (Phase 11 migration adds):**
- `px0-bridge → auth-service [internal:read, internal:write]`
- `px0-bridge → kafka [topic:px0.org.*:read, topic:cypherx.tenant.wipe.requested:write]`

**Failure modes:**
- px0 event arrives for a tenant CypherX already has → ON CONFLICT path; no error.
- Auth admin API down → Kafka offset NOT committed; retry on next consumer poll. Backed by Phase 1 Kafka DLQ after 10 attempts (per Phase 1 Component 17 convention).
- Conflicting `org.suspended` then late `org.created` (out-of-order) → bridge accepts both; final state is "suspended" because suspend is idempotent and won't be un-done.

---

### Component 7 — Cross-Service Quota Enforcement ⚡ (NEW)

Per-service quota counters (Phase 3/4/5/6/7 each maintain their own Valkey counters per Contract 19) work for per-service limits but cannot enforce **platform-wide** policies like "free-tier tenant cannot exceed $50/month aggregate across all services" or "enterprise-tenant has a 10K req/min ceiling across all services combined." Component 7 closes that gap.

**Architecture:**
- Subscriber to `cypherx.*.usage.recorded` from every service.
- Maintains `platform.tenant_running_totals` in Valkey (per-tenant, per-window, per-meter) — `requests`, `tokens`, `cost_usd`, `storage_bytes`.
- Exposes `POST /v1/quotas/check { tenant_id, units }` for services to consult before expensive operations (currently optional — services trust their own counters; centralised consult is for cross-service policies).
- Exposes `GET /v1/quotas/{tenant_id}/effective` returning the merged plan_defaults + tenant_quotas + current usage.
- Publishes `cypherx.tenant.quota.breached` when any aggregate threshold trips, with `meter`, `limit`, `current`, `window` payload — services consume this to apply 429/402 on the next call.

**Why not per-service-only:** an external tenant can chain operations (LLM call → memory store → tool call → another LLM call) that individually pass each service's quota but collectively bust the monthly cost cap. Cross-service aggregation prevents this.

**Schema:**
```sql
CREATE TABLE platform.tenant_running_totals (
  tenant_id    UUID NOT NULL,
  meter        VARCHAR(50) NOT NULL,             -- 'cost_usd', 'requests', 'tokens', 'storage_bytes'
  window       VARCHAR(20) NOT NULL,             -- 'minute', 'hour', 'day', 'month'
  window_start TIMESTAMPTZ NOT NULL,
  value        NUMERIC(20,8) NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, meter, window, window_start)
);
ALTER TABLE platform.tenant_running_totals ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant_running_totals ON platform.tenant_running_totals
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

Valkey hot-counter (60s flush to Postgres for durability): `quota:agg:{tenant_id}:{meter}:{window}` integer/float.

**Fail mode:** if Valkey is unavailable, fall back to Postgres last-flushed values (60s stale). If both unavailable, fail closed for `cost_usd` meter (avoid runaway spend), fail open with WARN for `requests` (avoid sudden 503 on a brief Valkey blip).

**Endpoints:**
```
GET    /v1/quotas/{tenant_id}/effective      Merged plan + override + current usage   [scope: tenant:read (self) or platform:admin]
POST   /v1/quotas/check                      Pre-flight check before expensive op     [scope: internal:read]
POST   /v1/admin/tenants/{tenant_id}/quotas/override   Set tenant_quotas override     [scope: platform:admin]
```

---

### K8s Deployment Spec

```yaml
Namespace:   platform-mgmt
Deployment:  platform-service
Replicas:    min 2, max 6 (HPA on CPU 70% — first-cycle minimum)
Node selector: node-role: core

Resources:
  requests: { cpu: 300m, memory: 512Mi }
  limits:   { cpu: 1500m, memory: 1Gi }

Startup probe (Postgres + Kafka warm-up):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 12          # 60s grace

Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches DB / Kafka / px0 / Slack.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness):
    #   - PostgreSQL reachable
    #   - Kafka reachable (platform-mgmt is event-driven)
    # Soft deps (log + metric only):
    #   - px0 billing API (only matters at month-end; per-attempt failures alert separately)
    #   - PagerDuty / Slack webhooks (alert-side failures, not platform-mgmt's readiness)

Env vars (from Doppler):
  DATABASE_URL                  (PgBouncer → platform schema, runtime user plat_user)
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AUTH_SERVICE_URL              (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                 (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET      (Contract 12; from service-auth/platform-mgmt/bootstrap_secret)
  ARGOCD_API_URL                (http://argocd-server.argocd.svc.cluster.local)
  ARGOCD_WEBHOOK_HMAC_KEY       (alerting/argocd_webhook_hmac_key)
  PX0_API_URL                   (https://api.px0.cypherx.ai)
  PX0_API_KEY                   (Doppler: px0/api_key)
  PAGERDUTY_SERVICE_KEY         (alerting/pagerduty/service_key)
  SLACK_WEBHOOK_PLATFORM        (alerting/slack/platform_webhook)
  SLACK_WEBHOOK_XAGENT          (alerting/slack/xagent_webhook)
  SLACK_WEBHOOK_BILLING         (alerting/slack/billing_webhook)
  GITHUB_APP_PRIVATE_KEY        (ci/github_app_private_key — for rollback path)
```

> **Service ACL (cross-phase Phase 2 update — Phase 11 migration adds):**
> - `platform-mgmt → auth-service [internal:read, internal:write]` (tenant + agent admin, audit reads)
> - `platform-mgmt → llms-gateway [internal:read]` (model list resolution for cost calc)
> - `px0-bridge   → auth-service [internal:read, internal:write]` (tenant CRUD)
>
> The ArgoCD webhook receiver uses a shared HMAC secret (`ARGOCD_WEBHOOK_HMAC_KEY`) — not a service-JWT — because ArgoCD doesn't speak Contract 12. The webhook path is internal-only (`/v1/internal/argocd-webhook`) and Istio AuthorizationPolicy restricts inbound to the `argocd` namespace.

> **JWKS verification** follows the Phase 3 standard: in-cluster URL only, 5-min cache, refresh-on-`kid`-miss rate-limited to 1/min.

---

## 📋 Full Enterprise Implementation Checklist

- [ ] Platform service architecture planned separately
- [ ] **Service registry as derived view** — sync from K8s EndpointSlices (30s health, 5-min discovery) + ArgoCD (5-min version); no external write API. `depends_on` is display-only (authoritative source is `auth.service_acl`).
- [ ] **Config store with append-only versioning** (`UNIQUE (service, env, key, version)`, `effective_from`); `platform.config_current` view for latest-version reads; `/history` returns real history; point-in-time read via `?at=<RFC3339>`
- [ ] **Config scope gating** — reads gated by `platform:read` OR `config:read:<service>`; writes by `platform:admin` OR `config:write:<service>`; all writes audit-logged
- [ ] **`platform.outbox`** + publisher loop + DLQ; transactional write for `cypherx.platform.config.updated` (and Component 6 events)
- [ ] **Per-pod consumer groups** for `cypherx.platform.config.updated` (every subscriber pod sees every update; matches Phase 10 broadcast pattern)
- [ ] **Kafka topics provisioned via Phase 11 Terraform:** `cypherx.platform.config.updated` (+ DLQ); `cypherx.tenant.wipe.requested` (+ DLQ)
- [ ] **Cost aggregator** consumes `cypherx.llms.request.completed` + `cypherx.billing.usage.recorded` (does NOT consume own output); UPSERT into `platform.tenant_costs`
- [ ] **`platform.tenant_costs` with generated `total_cost_usd` column + RLS** (tenant-scoped table per Contract 13); `period` is `DATE` (not `VARCHAR(7)`)
- [ ] **`k8s-compute-cost-emitter`** job: scrapes Prometheus, emits `cypherx.billing.usage.recorded` per tenant per hour
- [ ] **`s3-storage-cost-emitter`** job: scrapes S3 metrics, emits `cypherx.billing.usage.recorded` per tenant per day
- [ ] **`platform.billing_push_log`** table; px0 push uses `Idempotency-Key: billing-<tenant>-<period>` with exponential backoff (30s → 6hr, max 24h); critical alert on `attempts >= 5 for 1h`
- [ ] **`platform.deployments` populated by ArgoCD notifications webhook** (`POST /v1/internal/argocd-webhook` with HMAC verify); 5-min ArgoCD-API sync as backup
- [ ] **Rollback API** (`POST /v1/deployments/{service}/rollback`) — `platform:admin` only, rate-limited 1 per (service, env) per 5 min; reverts via cypherx-gitops-bot GitHub App; audit-logged
- [ ] **Component 5 alert rules** — every rule includes `summary` + `runbook_url` annotation; CI `prometheus-alerts-lint.yml` rejects PRs without
- [ ] **Per-environment cost anomaly thresholds** ($10 dev / $100 staging / $1000 prod); per-tenant z-score is 📋
- [ ] Alert routing config (Alertmanager) with Doppler-sourced secrets at documented paths
- [ ] Platform-wide cost dashboard in Grafana — **two data sources documented**: Prometheus gauge `cypherx_tenant_cost_usd_total` for last 24h; PostgreSQL `platform.tenant_costs` for historical/billing
- [ ] **Component 6 — px0 Tenant Bridge** (new service, ns `px0-bridge`) consuming `px0.org.created|suspended|deleted`; fans out `cypherx.tenant.wipe.requested` on delete; outbox-backed; ACL: `px0-bridge → auth-service`
- [ ] Cross-phase Service ACL extended (`platform-mgmt → auth-service/llms-gateway`, `px0-bridge → auth-service`)
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints; readiness gated on Postgres + Kafka; px0/PagerDuty/Slack soft
- [ ] **Startup probe** (60s grace)
- [ ] `AUTH_JWKS_URL` + `SERVICE_BOOTSTRAP_SECRET` env vars
- [ ] Deployed to K8s via ArgoCD

> 📋 deferred: per-tenant z-score cost anomaly detection (rolling 7-day window); auto-archive of `platform.config` rows older than 1 year; rollback flap-loop detection beyond rate limiting.

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Platform Management Becoming a God Service — REAL
Evidence: lines 7–36 (single service owns registry, config, billing, quotas, deployments).
**Mitigation:** extract quota enforcement (Component 7) into dedicated `quota-service` (isolated Valkey + PG table); move billing aggregation into `billing-aggregator` consuming same Kafka topics. Shared schema, decoupled deployments.

### 2. Synchronous Quota Check Bottleneck — REAL
Evidence: line 546 (sync `POST /v1/quotas/check`).
**Mitigation:** (a) per-service local quota cache (5 s TTL) with invalidation-on-breach event; (b) async pre-flight via `cypherx.tenant.quota.check.requested` topic; (c) fail-open with WARN if quota-service unavailable >60 s.

### 3. Billing Aggregation Idempotency Gap — REAL
Evidence: lines 246–250 (UPSERT without dedup key).
**Mitigation:** add `event_id UUID` + `UNIQUE (event_id, tenant_id, period)` to `platform.tenant_costs`; skip UPDATE if `event_id` already present. Alt: consumer-side Redis dedup cache (TTL 24 h) keyed by (topic, partition, offset).

### 4. Broadcast Config Propagation Will Not Scale — REAL
Evidence: line 178 (per-pod consumer groups; no versioning/ACK).
**Mitigation:** (a) add `version` to config event envelope; pods skip if `received_version >= stored_version`; (b) per-service ACK via `cypherx.platform.config.applied`; (c) consider compacted topic for config snapshots.

### 5. Rollback State Machine Operationally Incomplete — REAL
Evidence: lines 372–384 (happy-path only).
**Mitigation:** add `rollback_status` column (pending | pr_created | pr_merged | synced | failed). Async state machine: emit `cypherx.deployment.rollback.requested`; separate worker polls GitHub/ArgoCD until SYNCED or FAILED (60 s timeout → alert). Rate limit: one in-flight rollback per (service, env).

### 6. Service Registry Duplicates Kubernetes Health Authority — REAL
Evidence: lines 52, 62, 76–81 (dual sources).
**Mitigation:** poll K8s EndpointSlices (`Condition.type=Ready`) for status; reserve `/livez` polling for metrics only. If both polled, registry marks `degraded` (not `offline`) on `/livez` failure; alert when K8s and `/livez` disagree.

### 7. Missing Disaster Recovery and Bootstrap Architecture — REAL
Evidence: no DR section anywhere in doc.
**Mitigation (new section required):**
- **Cold-start order:** Auth → Kafka topics + px0-bridge → platform-mgmt (reconciles registry from K8s) → data services. Document in ops runbook.
- **Postgres failover:** explicit RTO/RPO SLA; read-replica promotion plan; hourly WAL archival to S3.
- **Kafka rebuild:** 7-day retention on `config.updated`; consumer lag backfill plan; DLQ replay procedure.
- **Platform-mgmt statefulness:** registry/config/deployments are derived/cached (K8s + ArgoCD are sources of truth) — safe to wipe platform schema and resync via 5-min reconcile jobs. Cost/billing tables are primary — require WAL recovery.

### 8. PostgreSQL Becoming an Overloaded Universal Control Plane DB — REAL
Evidence: schema namespaces concentrated (registry, config, billing, quotas, deployments + `auth.*`).
**Mitigation:** (a) separate PG instances (platform-db, auth-db) with FDW or async sync; (b) read-replicas for billing/quota reads; (c) async billing ingestion (Valkey → hourly PG flush); (d) PgBouncer transaction-mode per pool. Recommend (c)+(d) for first iteration.
