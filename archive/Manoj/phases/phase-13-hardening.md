# Phase 13 — Hardening & External Readiness
> **Status:** ⏳ Pending | **Depends On:** All phases complete | **Blocks:** Phase 14
> **First Cycle:** 📋 Not required for first cycle. Final phase before external access.

## Amendment Log (2026-06 — pre-build reconciliation)

- **Domain 3 rescoped to the quota single-owner rule.** Quota and rate-limit ENFORCEMENT logic is owned ⚡ first-cycle by the service phases themselves (Phase 3 LLMs token windows, Phase 5 RAG storage/operation quotas, Phase 6 Memory quotas — see their ⚡ checklists). Phase 13 owns NOTHING canonical here: this domain only **TUNES** limit/tier values from load-test results and **ADDS** cost-anomaly detection. The prior "canonical quota tiers — Phase 13 owns" claim and the "[DEFERRED FROM Phase 5/6] ... land here" quota items are DELETED (tombstoned in Domain 3 and the checklist); the tiers table's canonical home is `contracts/billing/tiers.yaml` in the Phase 0 contracts repo, not this phase.

---

## Phase Overview

Phase 13 takes a functionally complete platform and makes it **production-hardened**: security-audited, load-tested, rate-limit tuned, publicly documented, and ready for external developers. No new features are added — existing features are made rock-solid.

> 🏗️ **No service architecture planning needed here.** This phase operates on existing services. Each hardening activity is a cross-cutting concern.

---

## High Level Design

```
Hardening covers 9 domains (expanded from 6 to absorb deferrals from Phases 0–12):

  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
  │ 1. Security     │  │ 2. Performance & │  │ 3. Rate Limiting &   │
  │    Audit & WAF  │  │    Load Testing  │  │    Per-Tenant Quotas │
  └─────────────────┘  └──────────────────┘  └──────────────────────┘
  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
  │ 4. Public API   │  │ 5. Sandbox &     │  │ 6. Operational       │
  │    Documentation│  │    Marketplace   │  │    Readiness (SLO,   │
  │                 │  │                  │  │    status page, RBAC)│
  └─────────────────┘  └──────────────────┘  └──────────────────────┘
  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
  │ 7. Service      │  │ 8. Disaster      │  │ 9. Compliance        │
  │    Identity     │  │    Recovery &    │  │    Readiness         │
  │    Migration    │  │    Continuity    │  │    (SOC2/GDPR/ISO)   │
  │    (SPIFFE)     │  │                  │  │                      │
  └─────────────────┘  └──────────────────┘  └──────────────────────┘
```

> **Domains 7, 8, 9 absorb the items deferred to Phase 13 by Phases 0–12.** Don't skip them — Phase 0 Contract 12 named SPIFFE migration as a Phase 13 blocker, Phase 1 deferred CloudTrail/GuardDuty/WAF/multi-AZ here, Phase 11 deferred per-tenant z-score anomaly here. The previous 6-domain layout missed all of it.

---

## Low Level Design

> All items are 📋 — none required for first cycle.

---

### Domain 1 — Security Audit & WAF 📋

```
1. Penetration test (external security firm or internal red team)
   Focus areas:
     - JWT forging/tampering, header injection
     - JWKS rotation: rehearse on staging — multi-key window, Kong cache refresh,
       downstream service JWKS cache refresh; verify zero-downtime
     - JWT `kid` mismatch handling (refresh-on-miss rate-limited 1/min)
     - Cross-tenant penetration battery (Contract 13 cross-tenant denial test
       EXTENDED to every endpoint + every payload + every tenant-scoped resource —
       not just one test per table; per-tenant agent calling every other tenant's
       resources should always 403/404 with NO information leak in error body)
     - A2A spoofing — fake agent attempts to mint A2A token, chain-walk tampering,
       in-chain cycle (A→B→A), async cross-chain cycle via Kafka fan-out
     - A2A async callback HMAC drill — fuzz the HMAC verification, replay-attack
       window (timestamp skew > 5 min must reject), signature-comparison
       timing-side-channel (constant-time compare required)
     - Tool server escape (can tool-code-exec break out of gVisor?)
     - Prompt injection passing through guardrails (red-team prompts + adversarial
       suffixes; detoxify model evasion attempts)
     - SSRF via tool-http-client AND multimodal image_url fetch (Phase 3 post-edit
       extended SSRF guards to image fetches; verify)
     - Kong rate limit bypass (header injection, path normalisation tricks)
     - Bootstrap super-admin sentinel verification — after bootstrap, attempt to
       call /v1/admin/bootstrap again with the same token; MUST return 410 Gone

2. OWASP Top 10 review per service (automated + manual)

3. Dependency vulnerability scan (Trivy, Snyk)
   - Block deployment if CRITICAL CVEs found
   - Automated weekly scans in CI
   - Per-PR scans (already required by Phase 1 Component 18 post-edit — verify wired)

4. Secret audit
   - Confirm no secrets in code, git history, logs, or error messages
   - git-secrets / truffleHog scan on EVERY PR (CI rule), not just one-time audit
   - Verify Phase 4 secret-redaction CI test still passes for all services
     (Auth `auth.audit_log` MUST redact api_key/bootstrap_secret/etc per Phase 2 post-edit)

5. Certificate and TLS audit
   - All endpoints enforce TLS 1.3 minimum; TLS 1.2 allowed only for legacy SDK clients
     for first 6 months post-launch then dropped
   - Istio mTLS active (PeerAuthentication STRICT confirmed via mtlstest); only
     exception is the Phase 1 post-edit permissive-mode for Prometheus metrics ports
     (15020, 9090) — verify exception is exactly those two ports
   - JWKS endpoint is read-only (no write access); JWKS HTTP cache headers correct
   - All public domains carry HSTS preload + valid CT (Certificate Transparency)

6. IAM audit
   - All IRSA roles at minimum required permissions; cross-reference Phase 1
     post-edit TerraformInfraRole + TerraformIAMRole split (no role can modify itself)
   - No wildcard IAM policies; verify rag-service IRSA scoped to bucket prefix only
     (Phase 5 post-edit), BYOK IRSA scoped to per-tenant Secrets Manager paths
     (Phase 3 post-edit)
   - No unused access keys; GitHub Actions OIDC is the only entry point (no static
     AWS keys); Phase 1 GitHub-Actions IP allow-list explicitly forbidden
   - KMS key access review: cypherx-auth-signing, cypherx-rag-<env>,
     cypherx-byok-<env>, cypherx-tools-output-<env>, cypherx-a2a-output-<env>,
     cypherx-billing-output (Phase 11) — each restricted to its owning service IRSA

7. AWS WAF (deferred from Phase 1 Component 20 post-edit — MUST land here)
   - AWS WAF ACL attached to the public ALB
   - Managed rule groups: AWS OWASP Top 10, AWS Known Bad Inputs, AWS SQLi DB
   - Custom rate-based rules: 2000 req / 5 min per IP (free tier protection)
   - Geo-block list (only if business has regulatory restrictions; default off)
   - Deploy in MONITOR mode for 1 week; review false positives; then BLOCK mode
   - Logs to CloudWatch + alerts via Phase 11 alerting paths

8. AWS-side observability (deferred from Phase 1 Component 1 — ENABLE)
   - CloudTrail: enabled in all regions, 1-year retention (already provisioned in
     Phase 1; verify alerts wired to Phase 11 Slack #platform-alerts)
   - GuardDuty: enabled cluster-wide; threats route to Phase 11 PagerDuty
   - AWS Config: resource configuration history enabled; drift detection rules
     for IAM roles, S3 bucket policies, RDS encryption

9. Tenant wipe end-to-end drill (Phase 6 + Phase 11 post-edit verification)
   - Trigger px0.org.deleted event in staging.
   - Verify px0-bridge consumes, calls Auth DELETE /v1/admin/tenants/{id}, fans out
     cypherx.tenant.wipe.requested.
   - Verify every wipe-aware service consumes the event and runs its DELETE pass:
     Memory (gdpr_wipe_log row + DELETE), RAG (chunks + S3 objects),
     xAgent (tasks + task_steps + workflows + workflow_tasks),
     LLMs (usage_records archival), Guardrails (violations archival).
   - Verify cypherx.memory.gdpr.wiped + per-service wipe events appear in
     audit pipeline within 5 min.
   - Verify NO row remains in any tenant-scoped table for the wiped tenant_id —
     using the PLATFORM PATTERN, NOT an RLS-bypass role:
       For each tenant-scoped table T in {auth.agents, llms.usage_records,
       guardrails.violations, rag.knowledge_bases, rag.documents, rag.chunks,
       rag.chunk_vectors_<N>, memory.memories, memory.memory_vectors_<N>,
       memory.sessions, memory.gdpr_wipe_log, xagent.tasks, xagent.task_steps,
       xagent.workflows, xagent.workflow_tasks, ...}:
         BEGIN;
           SET LOCAL app.tenant_id = $wiped_tenant_id;   -- the wiped tenant's own context
           SELECT count(*) FROM T;
         COMMIT;
       Expected: count = 0 for every table.
     This exercises the EXACT RLS path the platform uses in production. No
     special BYPASSRLS Postgres role exists or is created — the Contract 13
     promise ("cross-tenant data access is architecturally impossible") MUST
     hold even for the drill operator. Anyone who proposes adding a BYPASSRLS
     role to "make verification easier" is breaking the security model the
     drill is supposed to verify.

10. REDACTION_HMAC_KEY rotation runbook (Phase 4 post-edit)
    - Quarterly rotation rehearsed on staging.
    - Verify old tokens (computed with old key) no longer correlate to new tokens
      for the same PII — intentional break of cross-time linkability.
    - Document operator steps: generate new key, store in Doppler, deploy
      guardrails-service, monitor for drift.

11. Doppler service-token rotation runbook (Phase 1 post-edit)
    - 90-day rotation rehearsed on staging.
    - During the transitional period (before SPIFFE migration — Domain 7), every
      service-auth/<service>/bootstrap_secret rotates every 30 days, not 90,
      because they're long-lived shared secrets in the meantime.

Deliverable: Security audit report, issue tracker with severity labels, all
CRITICAL/HIGH issues resolved before Phase 14.
```

---

### Domain 2 — Performance & Load Testing 📋

> **Targets recalibrated to actual call patterns.** Prior draft had "p99 < 50ms for /authorize" — but Phase 2 post-edit moved `/authorize` to layer-B (called rarely, only for stateful checks). The 50ms target was the wrong scale. xAgent `POST /v1/tasks` p99 — the actual end-user latency — wasn't listed at all. Rewritten below.

```
Tool: k6 (load test scripts) + Grafana k6 dashboard

Per-service SLO targets (canonical — every service's Prometheus rule alerts on these):

SharedCore/Auth:
  - 1000 concurrent JWT validations/sec sustained for 10 min (LOCAL via JWKS — no DB hit)
  - 100 agent registrations/sec burst (DB write)
  - 50 service-token mints/sec sustained (DB write + sign)
  - /authorize (LAYER-B only): p99 ≤ 200ms — note this is rare, NOT per-request
  - JWKS endpoint: p99 ≤ 20ms (cached at edge)

SharedCore/LLMs:
  - 500 concurrent chat completion requests, 100 concurrent streaming
  - Body-size cap (25 MiB multimodal) does not cause OOM under burst
  - Provider failover (when added in Phase 3 📋) recovers within 1s
  - Rate limit fires at the documented threshold (verify Retry-After header)
  - Outbox publisher backlog drains within 5 min after 10× burst (Phase 3 post-edit)

SharedCore/Guardrails:
  - 2000 concurrent input checks/sec
  - p99 ≤ 50ms for /check/input, ≤ 100ms for /check/output (Phase 4 post-edit SLO)
  - detoxify model serves under load without GC pauses
  - Outbox publisher backlog drains within 5 min after 10× burst

SharedCore/RAG:
  - 500 concurrent query requests, 50 concurrent ingestion jobs
  - Query p99 ≤ 200ms (two-pass CTE pattern — Phase 5 post-edit — verify HNSW used)
  - Ingestion does not starve queries (verify with concurrent run)
  - Per-tenant storage estimate (24 GiB per 1M chunks @ 1536 dims) does not surprise
    the operator under load (free-space alert fires at 70% before exhaustion)

SharedCore/Memory:
  - 1000 concurrent retrieves/sec, 200 concurrent stores
  - Batched TTL expiry (Phase 6 post-edit) does NOT cause write-lock storm during
    the hourly sweep (verify 10k-row batches + 1s pauses hold under load)

xAgent (THE end-user-facing latency):
  - 200 concurrent task submissions sustained
  - POST /v1/tasks (sync, single LLM call, no tools): p99 ≤ 5s, p50 ≤ 1.5s
    (LLM-bound; budget assumes Anthropic Sonnet p50 ~1s + 2× guardrails 30ms)
  - Per-task timeout enforcement (Phase 9 post-edit): in-pod context.WithTimeout
    fires before timeout_at; sweeper catches orphans within 30s
  - Cancellation propagation (Phase 10 post-edit per-pod consumer groups):
    DELETE /v1/workflows/{id} stops all subtask pods within 5s
  - Outbox publisher backlog drains within 5 min after 10× burst

A2A / Orchestration (Phase 10):
  - 100 concurrent A2A delegations, depth-3 chains
  - Chain-walk verification per A2A invoke: p99 ≤ 10ms
  - Async cross-chain cycle detection (50-agent threshold per root_task) fires
    correctly without false positives under burst

Kafka:
  - Simulate burst: 10,000 events/sec for 5 minutes
  - Verify consumer groups keep up (lag < 10,000)
  - Outbox publisher loops (Phases 3/4/5/6/9/10/11) all drain within 5 min after burst

Outbox publisher backlog test (cross-phase):
  - Inject 10× normal write rate to every service with an outbox for 30 min.
  - Verify backlog drains within 5 min after load drops.
  - Verify NO outbox row remains unpublished for > 5 min in steady state.
  - DLQ count after burst MUST be 0 (publisher retry should handle transient errors).

Report: latency percentiles, error rates, resource utilisation at peak, bottleneck
identification, outbox backlog timeline per service.

Production data-plane upgrades (deferred from Phase 1 — MUST land before external launch):
  - RDS multi-AZ + read replica for prod (Phase 1 Component 5 post-edit)
  - Valkey 3-node cluster for prod (Phase 1)
  - Kafka Schema Registry deployed; producers wired to enforce envelope/payload
    contracts (Phase 1 Component 15)
  - Multi-region RDS read replica (post-scaling, after SLO observation)

Performance follow-ups (deferred from other phases):
  - KEDA Prometheus-based scaler for llms-gateway on `llms_active_requests_per_pod
    > 50` or `llms_p95_latency_seconds > 5` (Phase 3 post-edit deferred)
  - KEDA scaler for xAgent on equivalent metrics
  - `auth.audit_log` monthly partitioning (Phase 2 post-edit deferred —
    high-volume table; PARTITION BY RANGE (created_at) — DETACH old partitions
    after 90 days for cold storage)
  - Async `last_accessed_at` tracker on memory.memories (Phase 6 post-edit:
    per-pod batch UPDATE every 5s) — land only if hot-write contention surfaces
    in prod load test
  - Embedded-library mode for guardrails (Phase 4 post-edit) — CONDITIONAL: land
    if guardrails RTT > 20% of xAgent task latency p50 in prod for 7 consecutive days
```

---

### Domain 3 — Rate Limiting & Per-Tenant Quotas 📋

> **AMENDED 2026-06 (single-owner rule — see Amendment Log):** quota and rate-limit
> ENFORCEMENT ships ⚡ first-cycle with the owning service phases (Phase 3 LLMs token
> windows, Phase 5 RAG storage/operation quotas, Phase 6 Memory quotas, Phase 7 tool
> manifests). Phase 13 owns NOTHING canonical: this domain only **TUNES** limit/tier
> VALUES from load-test results and **ADDS** cost-anomaly detection. The tiers table's
> canonical home is `contracts/billing/tiers.yaml` (Phase 0 contracts repo); every
> per-phase rate-limit code reads from it (or a shared config) — Phase 13 edits values
> in that file and never defines a second copy.

**Quota tier values (canonical home: `contracts/billing/tiers.yaml`, Phase 0 contracts repo — Phase 13 only tunes the numbers below from load-test results):**
```
┌──────────────┬──────────────┬──────────────┬──────────┬──────────────┬──────────────┐
│ Tier         │ requests/min │ tokens/min   │ agents   │ KBs (RAG)    │ memory rows  │
│              │ (per tenant) │ (per tenant) │ (max)    │ (max)        │ (max)        │
├──────────────┼──────────────┼──────────────┼──────────┼──────────────┼──────────────┤
│ free         │ 60           │ 10,000       │ 1        │ 1            │ 10,000       │
│ pro          │ 600          │ 100,000      │ 10       │ 10           │ 1,000,000    │
│ enterprise   │ negotiated   │ negotiated   │ unlim*   │ unlim*       │ unlim*       │
└──────────────┴──────────────┴──────────────┴──────────┴──────────────┴──────────────┘
* enterprise tier still has soft caps with operator alerts (no actual unlimited resource)
Storage estimate (RAG): 24 GiB per 1M chunks @ 1536 dims (Phase 5 post-edit) —
                        pro tier ~24 GiB max per KB; budget RDS accordingly.

Where the table lives: contracts/billing/tiers.yaml (Phase 0 contracts repo — the
canonical owner). Auth's authz cache loads it on startup; LLMs gateway, RAG, Memory,
xAgent all read from it via Auth /v1/tenants/{id}/limits (cached 60s). Phase 13
tunes VALUES in that file only.
```

**Per-layer enforcement (already wired ⚡ in the owning phases — Phase 13 tunes thresholds only):**
```
Kong level:     requests/min per route per consumer  (Phase 1 Component 8)
LLMs gateway:   token-bucket per tenant; FAIL-OPEN on Valkey outage with telemetry
                (Phase 3 post-edit)
Tool servers:   per-tool rate-limit from manifest (Phase 7 post-edit); same fail-open
Service level:  token-bucket per tenant + per agent
Valkey config:  rate-limit TTL windows tuned per service
```

**Per-tenant quotas:**
- (Tombstone 2026-06 — see Amendment Log: the prior "**Per-tenant RAG storage quotas**
  (Phase 5 post-edit deferred)" and "**Per-tenant memory row quotas** (implicit from
  Phase 6)" items claimed those quotas "land here". DELETED — both are ⚡ first-cycle
  ENFORCEMENT in Phases 5/6 (RAG: 413 `QUOTA_EXCEEDED` at ingest write time + Valkey
  op-rate windows; Memory: `memories_max`/`storage_bytes_max`/`stores_per_min`/
  `retrieves_per_min`). Phase 13 only TUNES their limit values and adds the 80%
  operator alert as part of threshold tuning.)
- **Per-tenant tool allowlist** (`registry.tenant_tool_acl` — Phase 7 post-edit
  deferred): first-cycle was all-tenants-see-all-tools; Phase 13 lands per-tenant
  filtering so a free-tier tenant can't invoke expensive enterprise-only tools.

**Per-tenant cost anomaly (deferred from Phase 11 post-edit):**
- 2-week observation period collecting real per-tenant token-spend distributions in prod
  BEFORE z-score thresholds are set (per-env fixed thresholds remain in place during
  observation).
- Then: rolling 7-day window z-score per (tenant, hour); alert when 3σ exceeded.
- Avoid false positives by gating on absolute floor too: $10/hr above baseline minimum
  before anomaly fires.

Tests: verify 429 responses fire at the TUNED limits, Retry-After headers correct,
the Phase 5/6-owned QUOTA_EXCEEDED enforcement still fires at the tuned resource caps,
cost anomaly fires within 2 hours of threshold breach, per-tenant tool ACL rejects
out-of-tier tool invocations.
```

---

### Domain 4 — Public API Documentation 📋

```
Tool: Redoc or Stoplight (OpenAPI 3.1 → hosted docs)

Documentation site: docs.cypherx.ai
  Structure:
    ├── Getting Started       (quickstart: register agent, get token, submit task)
    ├── Authentication        (JWT, API keys, scopes)
    ├── SharedCore
    │   ├── Auth API
    │   ├── LLMs API
    │   ├── Guardrails API
    │   ├── Memory API
    │   └── RAG API
    ├── xAgent
    │   ├── Tasks API
    │   ├── Workflows API
    │   └── A2A Protocol
    ├── Tools (MCP)           (MCP protocol docs, tool manifests)
    ├── Skills                (skill schema, how to author a skill)
    └── Webhook Reference     (Kafka event schemas for consumers)

API playground: embedded Swagger UI for each service (staging env keys)

Hosting (reuses Phase 12 module — no new Terraform module needed):
  - Static site (Redoc / Stoplight build output) → S3 + CloudFront via the
    `terraform/modules/frontend/` Terraform module introduced in Phase 12,
    instantiated with different inputs per env:
      bucket:        cypherx-docs-<env>
      distribution:  cypherx-docs-<env>-cf
      hostname:      docs.<env>.cypherx.ai  (per Phase 1 env-scoped DNS convention)
      prod alias:    docs.cypherx.ai
      ACM cert:      us-east-1 (CloudFront requirement), per-env wildcard
  - GH OIDC deploy role: same pattern as Phase 12 SPA — a `CypherX-DocsDeployerRole`
    per env with `s3:PutObject` + `cloudfront:CreateInvalidation` scoped to the
    docs bucket/distribution only.
  - PR previews: deploy to s3://cypherx-docs-preview/<pr-number>/ with a
    CloudFront behaviour rewriting that path (matches the Phase 12 SPA preview
    convention; 30-day lifecycle).
  - No new backend — docs are static; the API playground hits staging Kong
    directly via CORS (staging Kong allows docs.<env>.cypherx.ai in its CORS
    origin list ONLY for /v1/* paths that are documented; cross-checked via
    OpenAPI tag at build time).
```

---

### Domain 5 — Sandbox & Marketplace 📋

**Sandbox isolation (prior draft was "separate namespace" — not strong enough for external developers):**
```
SANDBOX HOSTED IN A SEPARATE AWS ACCOUNT + SEPARATE EKS CLUSTER.
  - Account:   cypherx-ai-sandbox  (NOT cypherx-ai)
  - Cluster:   cypherx-sandbox    (own VPC, own RDS, own MSK, own Valkey)
  - DNS:       sandbox.cypherx.ai (separate Route53 zone delegation)

Why a separate account, not just a namespace:
  - K8s namespace isolation does NOT protect against shared-cluster zero-days
    (kernel escapes, etcd compromise, cluster-admin RBAC mistakes).
  - External developers will probe aggressively. Blast radius MUST be a SEPARATE
    AWS account so the worst outcome is "sandbox is gone for 24h", not "prod is
    compromised".
  - Promotes from staging cluster build artefacts; identical service code; nothing
    shared at the data plane.

Sandbox configuration:
  - Free API keys issued to developers for testing (sandbox-only; do not work in prod)
  - Real platform, isolated data (no prod data ever)
  - Rate limits: very low (free tier limits ÷ 10 — protect sandbox infra)
  - Auto-cleanup: sandbox data purged every 7 days (CronJob runs the full
    cypherx.tenant.wipe.requested flow per sandbox-tenant; Domain 1 wipe drill
    rehearses this monthly in staging)
  - Disabled: billing push to px0 (Phase 11 billing aggregator no-ops in sandbox env)
  - Disabled: AWS WAF block mode (monitor only, so devs can debug their requests
    without WAF false positives masking real bugs)
```

**Agent Marketplace v1:**
```
A publicly browsable catalogue of available agents:
  GET /v1/marketplace/agents
  → Returns: public agent definitions (name, description, capabilities, version)
  Public agents marked via auth.agents.marketplace_public = true (new column,
    default false; flipping requires platform:admin review).

Developers can:
  - Browse agents in the marketplace
  - Fork an agent definition to their org (creates new auth.agents row + xagent.agents
    runtime with the same system_prompt; tenant_id = forker's tenant)
  - Submit their agent to marketplace (manual review process; submission status
    tracked in auth.agents.marketplace_submission_status enum)

This is Phase 13 v1 — full marketplace features (rating, reviews, fork analytics)
move to Phase 14/post-SDK.
```

---

### Domain 6 — Operational Readiness (SLO, Status Page, On-Call) 📋

```
Service-level objectives (SLO) — documented per service, alert on breach:

  Each service publishes its SLO doc at runbooks.cypherx.ai/slo/<service>.
  SLO targets are sourced from Domain 2 load-test targets; this domain wires the
  alerting + error budgets.

  Error budget policy:
    - 99.9% availability per service per month = 43 min downtime budget.
    - If budget consumed early in the month → freeze non-critical deploys for
      that service until next month (or until budget recovers).
    - Tracked in Grafana per service; visible to engineering + product.

Status page (deferred from prior draft — now designed):

  Tool: Betterstack (or equivalent).
  Public URL: status.cypherx.ai
  Data flow (FULLY AUTOMATED):
    Prometheus alert (severity=critical, type=availability)
      → Alertmanager
      → Betterstack webhook
      → Auto-creates incident on the affected component(s)
      → Status auto-resolves when underlying alert clears for ≥ 5 min.

  Components displayed (matches end-user-visible surface, not internal services):
    - API (api.cypherx.ai)         ← aggregates Auth + xAgent + LLMs + Guardrails
    - Dashboard (app.cypherx.ai)   ← frontend + BFF
    - Documentation (docs.cypherx.ai)
    - Sandbox (sandbox.cypherx.ai)

  Manual override: platform-mgmt admins can post a manual incident
  (e.g., scheduled maintenance) via the Betterstack UI; auto-resolution paused.

On-call rotation:
  PagerDuty schedules from Phase 11 alerting paths.
  Primary on-call carries `pagerduty-primary` mobile.
  Secondary escalates after 15 min unacknowledged.
  Each alert MUST have a `runbook_url` annotation (CI-enforced — Phase 11 post-edit).

Incident post-mortems:
  Within 5 business days of any SEV-1 / SEV-2 incident.
  Template in docs/runbooks/postmortem-template.md.
  Action items tracked in a dedicated linear/jira project; closed-loop on every action.

RBAC for ops surfaces (defence in depth on top of Phase 2 RBAC):
  - Status-page admin: small named set (platform team leads).
  - ArgoCD prod sync approver: rotating named set (NOT the same person who wrote the PR).
  - Doppler prod-config write: small named set; rotated yearly.
```

---

### Domain 7 — Service Identity Migration (SPIFFE) 📋

> **Phase 0 Contract 12 (post-edit) explicitly named this as a Phase 13 blocker:**
> "Bootstrap-secret mode MUST be disabled in production by Phase 13. This is tracked
> as a hardening blocker, not a first-cycle blocker." Without this migration, every
> service-to-service call still authenticates with a shared static secret in
> production — exactly the model Contract 12 said was unacceptable post-first-cycle.

```
Migration plan (cutover, NOT a long parallel-run):

Step 1 — SPIFFE infrastructure (1 week):
  - Deploy SPIRE server in spire-system namespace (CA-of-CAs role).
  - Deploy SPIRE agent as DaemonSet (one per node) — issues SVIDs via Workload API.
  - Configure SPIRE to back per-namespace identities to K8s service accounts.
  - SPIFFE trust domain: cypherx.<env>.svc (one per environment).
  - SVIDs are short-lived (5 min); auto-rotated by SPIRE agent in-process.

Step 2 — Auth service supports both modes (1 week):
  - Existing POST /v1/service-tokens accepts X-Service-Bootstrap-Secret (bootstrap).
  - NEW path: POST /v1/service-tokens accepts a SPIFFE SVID (TokenReview against
    the cluster API + SPIFFE identity verification).
  - Both produce identical service-JWT output (Contract 12). Per-target `aud`
    scoping enabled in this path (no more `aud=["*"]`).

Step 3 — Per-service migration (2 weeks, one service per day):
  - Each service's deployment manifest adds a SPIFFE workload selector + Workload
    API socket mount.
  - Service's startup code: try SPIFFE first; fall back to bootstrap-secret if
    SVID unavailable (transitional safety net).
  - Migrate in this order (lowest blast radius first):
    frontend-bff → tool-* → memory → rag → guardrails → llms-gateway → a2a-router →
    xagent → auth-service (self-migration last; chicken-and-egg careful).
  - Per-service rollback: if SPIFFE path fails, fall back to bootstrap-secret
    (transitional only — must not stay in this state).

  > **SCOPE — IN-CLUSTER ONLY (skills-ci and any other external CI workflow excluded):**
  > SPIFFE requires a SPIRE agent on the workload's node. GitHub-hosted runners are
  > not nodes we control, so workflows like `skills-ci` (Phase 8) cannot host a SPIRE
  > agent. Those workflows REMAIN on the GH OIDC → AWS Secrets Manager → bootstrap-
  > secret path established in Phase 8 / Phase 1 Component 18. They are explicitly
  > out of scope for this SPIFFE migration.
  > Step 4's bootstrap-secret kill-switch (auth.feature_flags) applies to
  > IN-CLUSTER services only — CI workflows continue to mint service-JWTs via
  > `cypherx/ci/<workflow>/bootstrap_secret`, on the standard 90-day rotation
  > (not the transitional 30-day cadence which applied only to in-cluster bootstrap
  > secrets pre-SPIFFE).

Step 4 — Cutover (1 day):
  - Once every in-cluster service has run on SPIFFE for 7 days in prod, disable the
    bootstrap-secret path by flipping the kill-switch in `auth.feature_flags`
    (schema defined in the Phase 13 migration directory — see Cross-phase Resources
    block at end of this phase):
      INSERT INTO auth.feature_flags (key, value, updated_by, updated_at)
      VALUES ('bootstrap_secret_enabled', 'false', '<operator agent_id>', NOW())
      ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
            updated_at = NOW();
    Auth's `POST /v1/service-tokens` handler checks this flag at request time
    (cached in-process 30s). When `false`, bootstrap-secret-mode requests return
    `403 BOOTSTRAP_SECRET_DISABLED`; SPIFFE-mode requests continue to work.
  - All `service-auth/<svc>/bootstrap_secret` rows for IN-CLUSTER services in
    Doppler can be deleted (or moved to glacier for incident-recovery break-glass).
    `cypherx/ci/<workflow>/bootstrap_secret` in Secrets Manager (external CI)
    is NOT affected by this kill-switch — see Step 3 scope note above.

Step 5 — Verification:
  - Audit: every service-token issuance log line MUST show source=SPIFFE.
  - Penetration test: attempt to mint a service token using a stolen bootstrap
    secret — MUST be rejected.
  - Documentation update: Contract 12 marked SPIFFE as canonical;
    bootstrap-secret marked deprecated.

Per-target `aud` scoping (also deferred from Contract 12 — lands with SPIFFE):
  - First-cycle minted aud=["*"]. Now: aud=[<target-service-name>] explicitly.
  - service_acl rows already specify caller→target; Auth uses them to narrow aud.
  - Compromised service tokens limited to their declared call graph.

Delegation chain SPIFFE upgrade (Phase 10 cross-link):
  - A2A delegation_chain entries (Phase 10 Component 2) carry agent identity, not
    service identity. SPIFFE migration does NOT change A2A semantics — A2A is
    agent-to-agent, governed by chain validation, not service ACL.
  - HOWEVER: a2a-router's own service identity migrates to SPIFFE in Step 3.

Rollback rule:
  - SPIFFE migration is single-direction. Once Step 4 disables bootstrap-secret,
    rolling back requires explicit operator action (re-enable flag, re-populate
    Doppler secrets). NOT a deploy-time rollback.
```

---

### Domain 8 — Disaster Recovery & Business Continuity 📋

> **Without this domain the platform is one `rm -rf` away from total loss.** The
> previous Phase 13 had zero mention of backup, recovery, or RTO/RPO targets.

```
RTO / RPO targets:
  Production:  RTO ≤ 4h, RPO ≤ 15min
  Staging:     RTO ≤ 24h, RPO ≤ 1h
  Dev:         best-effort

What is backed up:

  RDS (PostgreSQL):
    - Automated daily snapshots (Phase 1 — 7-day retention default).
    - Point-in-Time Recovery: 7-day window (matches retention). Same-region RPO
      via PITR is effectively ~5 min, comfortably under the 15 min target.
    - **CROSS-REGION continuous replication for prod (MANDATORY to meet 15 min RPO
      across regions):**
        Option A (preferred): Aurora PostgreSQL Global Database — writes in primary
                              region replicate to DR-region read replica with
                              typical lag < 1 second. Failover by promoting the
                              DR replica (≤ 1 min via console / RDS API).
        Option B (fallback if Phase 1 RDS is not Aurora): logical replication via
                              AWS DMS or a managed replication slot to a DR-region
                              standby; lag typically < 30 seconds; failover via
                              Route53 + connection-string swap.
      Prerequisite check: confirm Phase 1's RDS choice supports Option A. If not,
      promoting to Aurora is a Phase 13 prerequisite (one-time logical migration;
      schedule before the cross-region drill). Without continuous replication, the
      Q3 cross-region failover drill CANNOT meet the 15 min RPO target — daily
      snapshots can lose up to 24 h of writes.
    - **DR-region snapshot copy (secondary backup, daily, 30-day retention):** insurance
      for the "DR region itself lost" scenario. Daily cadence is fine for THIS layer
      because the continuous-replication layer above provides the active RPO; this
      tier exists only as a last-resort restore source.

  S3 buckets:
    - cypherx-rag-<env>:           versioning ON, 7d noncurrent retention,
                                   cross-region replication (CRR) for prod
                                   (replicates to cypherx-rag-<env>-dr region).
    - cypherx-billing-output:      versioning ON, CRR for prod
                                   (financial records — must survive single region loss).
    - cypherx-tools-output-<env>:  no backup (24h lifecycle; transient).
    - cypherx-a2a-output-<env>:    no backup (24h lifecycle; transient).
    - cypherx-byok/<tenant>/<provider>: NOT backed up by us (Secrets Manager has
                                   its own backup; we hold only the key ARN).
    - cypherx-terraform-state-<acct>: versioning ON (existing per Phase 1).

  Kafka (MSK):
    - Per-topic retention is the primary backup (delete-policy topics) or compaction
      (compact-policy topics — these are forever).
    - MirrorMaker 2 to a DR region for prod (last 30 days of delete-policy topics
      + all compact-policy topics).
    - Outbox tables in Postgres are the durability fallback — if Kafka is lost
      entirely, every outbox row CAN be replayed from Postgres state. Verify this
      in DR drill (Step 4 below).

  Doppler:
    - Doppler's own backup (relied upon — vendor-managed).
    - Per-env config export to encrypted S3 daily (insurance against Doppler outage).

  KMS keys (cypherx-auth-signing, cypherx-rag, cypherx-byok, etc.):
    - Multi-region keys for prod (AWS KMS feature — same key in DR region).
    - Key material NEVER exfiltrated (AWS KMS does not expose CMK material;
      hardware-backed).

DR drill cadence:
  Quarterly in prod, monthly in staging. Each drill exercises ONE scenario:

  Q1: RDS PITR drill
      - Restore prod RDS snapshot to a fresh instance in the SAME region.
      - Point a clone xAgent service at the restored DB.
      - Run smoke test from Contract 15 (post-edit) against the clone.
      - Document time-to-restore + time-to-smoke-test-pass.
      - Acceptance: total ≤ RTO target.

  Q2: Cluster rebuild drill (the BIG one)
      - Stand up a brand-new EKS cluster in the same region (or DR region).
      - ArgoCD sync from cypherx-gitops repo → entire platform redeployed.
      - RDS restored from snapshot, MSK restored from MirrorMaker, S3 from CRR.
      - Run Contract 15 smoke test end-to-end.
      - Acceptance: total ≤ RTO target; this is the killer feature — proves the
        cluster is truly stateless except for data plane.

  Q3: Cross-region failover drill (prod-grade scenario)
      - Simulate primary-region outage (network ACL block on AZ).
      - Switch Route53 to DR region; promote DR-region RDS replica (Aurora Global
        Database failover OR DMS-replica promotion); bring up cluster from CRR
        + restored MSK MirrorMaker state.
      - Acceptance: total ≤ 4 h RTO; **replication lag at failover time ≤ 15 min
        (the RPO target)**; verify DNS TTLs are short enough (60s on the failover
        Route53 records). If lag > 15 min, the continuous-replication layer is
        misconfigured — that is the failure mode to detect in this drill.

  Q4: Tenant-wipe drill (Domain 1 #9 — also runs in Q4 as a recovery scenario)
      - Wipe a sandbox tenant; restore from backup to a separate restore-tenant; verify
        data integrity. Tests that backup + GDPR wipe interact correctly.

Outbox replay drill (Kafka loss scenario):
  - Simulate total MSK loss.
  - Bring up fresh MSK; reset all consumer offsets.
  - Re-run every outbox publisher loop (Phases 3/4/5/6/9/10/11) — they re-publish
    every unpublished row.
  - Acceptance: every event that should have been published IS published; no duplicates
    cause downstream incorrectness (Idempotency-Key on billing push prevents
    double-billing — verify per Phase 11 post-edit).

Documentation:
  - DR runbook at runbooks.cypherx.ai/dr — step-by-step for each scenario.
  - Reviewed quarterly; updated after every drill.
```

---

### Domain 9 — Compliance Readiness (SOC 2 / GDPR / ISO 27001) 📋

```
The platform's compliance-relevant patterns are already implemented across phases:
  - Audit logs (Phase 2 Component 6 + secret redaction per Phase 2 post-edit)
  - GDPR wipe (Phase 6 + Phase 11 px0-bridge fan-out + Domain 1 drill)
  - Redaction (Phase 4 + REDACTION_HMAC_KEY rotation)
  - RLS multi-tenant isolation (Contract 13 + cross-tenant denial CI tests)
  - mTLS in mesh, TLS everywhere (Phase 1 Istio STRICT + Domain 1 audit)
  - KMS encryption at rest (every database + S3 bucket)
  - SOC2 CC controls map to existing platform features

Phase 13 closes the loop with a readiness review, NOT new infrastructure.

SOC 2 Type 1 readiness:
  - Controls mapping: every Trust Services Criterion (TSC) mapped to an existing
    platform feature, runbook, or policy. Gap analysis identifies any TSC without
    a control (CC1.1 governance, CC6.1 logical access, etc.).
  - External audit (Big-4 firm or specialised SOC 2 firm); 3-month engagement.
  - Audit period: at least 3 months of evidence collected before audit start.

GDPR readiness:
  - Data Protection Impact Assessment (DPIA) for the platform as a whole.
  - Data-flow map: every PII path (Phase 4 redaction inputs, Phase 6 user-scope
    memories, Phase 5 RAG ingested content) documented with: data owner, lawful
    basis, retention, location, encryption at rest + in transit.
  - Data subject access request (DSAR) flow — tenant admin can export all data
    for a given user_id within 30 days. NOT a reuse of the Phase 6 wipe
    machinery (wipe is DELETE-only). New cross-service aggregator pattern:

    Endpoint (lives on platform-mgmt — Phase 11 — same as the audit-export endpoint):
      POST /v1/admin/dsar
      Auth:  user JWT with `platform:admin` OR `tenant:admin` scope
             (matches Phase 12 cross-tenant admin pattern; on_behalf_of records
             the requesting human)
      Body:  { "tenant_id": "<uuid>", "user_id": "<uuid>",
               "deadline": "2026-06-15T00:00:00Z" }
      Returns 202 with `request_id` for polling.

    Internal fan-out (mirrors the wipe pattern but READ instead of DELETE):
      1. platform-mgmt INSERT row into `platform.dsar_requests`
         (request_id, tenant_id, user_id, requested_by_user_id, deadline,
          status='pending', created_at).
      2. Publish `cypherx.tenant.dsar.requested` to Kafka with payload
         { request_id, tenant_id, user_id, request_id, trace_id }.
      3. Every DSAR-aware service consumes the topic with its own consumer group:
         - Memory:     SELECT memories WHERE scope='user' AND scope_id=$user_id;
                       write JSON-Lines to S3 cypherx-dsar-output-<env>/<request_id>/memory.jsonl
         - RAG:        SELECT documents WHERE metadata @> '{"user_id":"<user_id>"}';
                       write to .../rag.jsonl + copy doc S3 objects
         - xAgent:     SELECT tasks WHERE input metadata.user_id=$user_id;
                       write to .../xagent_tasks.jsonl
         - Auth:       SELECT audit_log WHERE actor_user_id=$user_id;
                       write to .../auth_audit.jsonl
         - Billing:    SELECT usage_records WHERE on_behalf_of=$user_id;
                       write to .../billing.jsonl
         Each service publishes `cypherx.<service>.dsar.exported` with
         { request_id, s3_uri, row_count } when done.
      4. platform-mgmt consumes all `*.dsar.exported` events; when ALL expected
         services reported (per a static list of DSAR-aware services in config),
         transitions the request to `completed`, generates a single ZIP manifest
         at S3 cypherx-dsar-output-<env>/<request_id>/manifest.zip, returns
         pre-signed URL valid 7 days.
      5. SLA enforcement: a CronJob alerts if `status='pending'` AND
         `created_at < NOW() - INTERVAL '25 days'` (5-day buffer before the GDPR
         30-day deadline). Sweeper escalates to PagerDuty.

    Schema additions (lives in Phase 13 migration directory):
      CREATE TABLE platform.dsar_requests (
        request_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id             UUID NOT NULL,
        user_id               UUID NOT NULL,
        requested_by_user_id  UUID NOT NULL,
        deadline              TIMESTAMPTZ NOT NULL,
        status                VARCHAR(20) NOT NULL DEFAULT 'pending',
                              -- pending | in_progress | completed | failed
        services_reported     TEXT[] NOT NULL DEFAULT '{}',
        s3_manifest_uri       TEXT,
        created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at          TIMESTAMPTZ
      );
      ALTER TABLE platform.dsar_requests ENABLE ROW LEVEL SECURITY;
      CREATE POLICY dsar_tenant_isolation ON platform.dsar_requests FOR ALL
        USING (tenant_id = current_setting('app.tenant_id')::uuid);

    Kafka topic (Phase 13 Terraform provisions):
      cypherx.tenant.dsar.requested        partitions: 3, retention: 30 days
      cypherx.tenant.dsar.exported.dlq     (DLQ pair)

    Per-service DSAR consumers are owned by their respective service teams;
    Phase 13's responsibility is the topic, the aggregator, the SLA sweeper,
    and the auth/UX layer. Each DSAR-aware service ships its consumer as part
    of its own Phase 13 hardening pass.
  - DPA agreements signed with all sub-processors: Anthropic, OpenAI, AWS, Doppler,
    px0, Betterstack, PagerDuty, GitHub.

ISO 27001 readiness (optional; many enterprise tenants require):
  - Statement of Applicability (SoA) mapping ISO controls to platform features.
  - Information Security Management System (ISMS) documentation.
  - Mostly overlaps with SOC 2 — same evidence, different paperwork.

Compliance-driven changes that ARE new infrastructure (rather than just docs):
  - Customer-facing audit-log export API (`GET /v1/admin/audit/export` with date
    range + tenant filter; produces JSON-Lines to S3 with pre-signed download URL)
    — for tenant SOC audits.
  - Per-tenant data residency advertisement (which AWS region a tenant's data lives
    in — visible in tenant dashboard; required by many EU customers).
  - Configurable data retention per tenant (override platform defaults; required
    by some healthcare/finance customers).
```

---

### Cross-phase resources owned by Phase 13

Phase 13 is mostly process (drills, audits, runbooks), but several domains DO
add code/config/infra. Grouped under one directory + Terraform module set for
reviewability — same pattern Phases 8, 10, and 12 established.

**1. Migrations directory — `platform-migrations/phase-13/`:**

```
platform-migrations/phase-13/
  ├── 20261001_0900__auth_feature_flags.sql          → auth.feature_flags
  │     (generic kill-switch table; Domain 7 uses it for bootstrap_secret_enabled)
  ├── 20261001_0901__registry_tenant_tool_acl.sql    → registry.tenant_tool_acl
  │     ([DEFERRED FROM Phase 7] per-tenant tool allowlist; enforced by tool registry
  │      and Kong at invoke time; default tier→tools mapping seeded from
  │      contracts/billing/tiers.yaml)
  ├── 20261001_0902__platform_dsar_requests.sql      → platform.dsar_requests
  │     (DSAR aggregator state — Domain 9; RLS-isolated)
  ├── 20261001_0903__audit_export_scope.sql          → auth scopes + default policy
  │     (registers audit:export and dsar:request scopes; granted to platform-mgmt
  │      service identity; deny-by-default for regular agents)
  └── README.md

DDL for auth.feature_flags (referenced by Domain 7 Step 4):
  CREATE TABLE auth.feature_flags (
    key         VARCHAR(100) PRIMARY KEY,
    value       TEXT         NOT NULL,
    description TEXT,
    updated_by  UUID         NOT NULL,            -- agent_id of the operator
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
  );
  -- Platform-internal — no RLS (cluster-wide config). Mutations require
  -- platform:admin scope; reads cached 30s in every Auth pod.
  -- Seed: ('bootstrap_secret_enabled', 'true', 'In-cluster bootstrap-secret
  --        service-token mode. Flip to false after SPIFFE migration.', ...)
```

Runtime:
- Applied as Atlas migrations under the platform-admin DDL credential.
- Pre-install K8s Job (`helm.sh/hook: pre-install,pre-upgrade`).
- All idempotent (`CREATE TABLE IF NOT EXISTS`, `INSERT ... ON CONFLICT DO NOTHING`).

Review:
- CODEOWNERS for `platform-migrations/phase-13/` requires Auth + Platform +
  **Security** team approval (three-team gate — this phase touches security-
  critical surfaces).
- CI runs the migrations against a real Postgres + integration tests for the
  feature-flag kill-switch and DSAR-aggregator paths.

**2. Terraform additions:**

```
terraform/modules/sandbox/                       (NEW — Domain 5)
  Provisions the entire cypherx-ai-sandbox AWS account topology:
    - Separate AWS account (Organizations API)
    - Separate VPC (10.10.0.0/16 — distinct from prod 10.0.0.0/16)
    - Separate EKS cluster cypherx-sandbox
    - Separate RDS / MSK / Valkey / S3 buckets
    - Cross-account ECR image-pull access from prod account
      (ECR repository policy on prod-account ECR allows cypherx-ai-sandbox
       account to pull; sandbox cluster's node IAM role can call sts:AssumeRole
       into a read-only cross-account role in the prod account)
    - Separate Route53 zone delegation for sandbox.cypherx.ai
    - Separate KMS keys (cypherx-rag-sandbox, etc.) — NO key shared with prod
    - WAF in MONITOR mode (Domain 1 #7 prod-side; sandbox stays in monitor-only)

terraform/modules/dr/                            (NEW — Domain 8)
  Provisions DR-region topology:
    - Aurora Global Database secondary cluster (if Phase 1 RDS upgraded to Aurora;
      otherwise DMS replica + DR-region standby per Domain 8 Option B)
    - MSK MirrorMaker 2 replication
    - S3 CRR for cypherx-rag-<env> + cypherx-billing-output
    - Multi-region KMS keys
    - Pre-provisioned EKS cluster shell in DR region (zero nodes; gitops-ready)

Reuse from Phase 12 (NO new module needed):
  - terraform/modules/frontend/ instantiated TWICE more:
      docs.<env>.cypherx.ai     (Domain 4 — docs site)
      status.<env>.cypherx.ai   (Domain 6 — Betterstack public component embed; OPTIONAL)
```

**3. Canonical config files (NOT migrations — version-controlled YAML):**

```
contracts/billing/tiers.yaml      (Domain 3 — canonical quota table; every
                                   rate-limiting service reads via Auth
                                   /v1/tenants/{id}/limits)
contracts/sre/slo.yaml             (Domain 6 — per-service SLO targets;
                                   Prometheus recording rules generated from this)
```

Owner: Phase 0 contracts repo; populated and rolled out as part of Phase 13.

---

## 📋 Full Enterprise Implementation Checklist

> Reorganised around the 9 domains. Items prefixed `[DEFERRED FROM X]` are explicit
> hand-offs from earlier phases that named Phase 13 as their target.

**Domain 1 — Security Audit & WAF:**
- [ ] Penetration test completed (external firm); report reviewed; all CRITICAL/HIGH findings resolved
- [ ] **Cross-tenant penetration battery** — every endpoint × every tenant-scoped resource (Contract 13 extension)
- [ ] **A2A async callback HMAC drill** — fuzz HMAC, replay window, constant-time compare (Phase 10)
- [ ] **JWKS rotation rehearsal** on staging; zero-downtime confirmed (Phase 2 post-edit)
- [ ] **Bootstrap super-admin sentinel verification** — re-bootstrap attempt returns 410 Gone (Phase 2 post-edit)
- [ ] **Tenant wipe end-to-end drill** — px0.org.deleted → all-service DELETE within 5 min (Phase 6 + Phase 11)
- [ ] **REDACTION_HMAC_KEY rotation runbook** rehearsed; cross-time linkability breaks (Phase 4 post-edit)
- [ ] **Doppler service-token rotation runbook** — 30-day cadence during SPIFFE transition (Phase 1 post-edit)
- [ ] OWASP Top 10 automated scan in CI (block on failure)
- [ ] Dependency scanning (Trivy) blocking on CRITICAL CVEs; weekly scheduled scan
- [ ] git-secrets / truffleHog scan on EVERY PR (CI rule)
- [ ] **Per-PR secret-redaction CI test** still passes for all services (Phase 2 post-edit)
- [ ] TLS audit — TLS 1.3 minimum for prod; 1.2 allowed only for legacy SDK clients (6-month sunset)
- [ ] Istio mTLS STRICT confirmed (mtlstest); permissive exception scoped to ports 15020 + 9090 only (Phase 1 post-edit)
- [ ] IAM audit completed (no wildcard policies); Phase 1 TerraformInfraRole/TerraformIAMRole split verified
- [ ] KMS key access review (auth-signing, rag, byok, tools-output, a2a-output, billing-output) — each scoped to owning IRSA
- [ ] **[DEFERRED FROM Phase 1] AWS WAF** attached to public ALB; monitor mode 1 week → block mode
- [ ] **[DEFERRED FROM Phase 1] CloudTrail enabled** (all regions, 1-year retention, alerts to Phase 11)
- [ ] **[DEFERRED FROM Phase 1] GuardDuty enabled** (threats route to PagerDuty)
- [ ] **[DEFERRED FROM Phase 1] AWS Config enabled** (drift detection on IAM, S3 policies, RDS encryption)

**Domain 2 — Performance & Load Testing:**
- [ ] k6 load tests written for all services using the **recalibrated SLO targets** above
- [ ] All services pass load test targets (p99 within spec)
- [ ] **xAgent `POST /v1/tasks` p99 ≤ 5s** under 200 concurrent task submissions
- [ ] **Outbox publisher backlog test** — 10× burst for 30 min; backlog drains within 5 min after load drops; zero rows in DLQ in steady state
- [ ] Kafka consumer lag verified stable under load (< 10,000)
- [ ] Bottlenecks identified and addressed (DB indexes, connection pool sizing, HPA tuning)
- [ ] **[DEFERRED FROM Phase 1] RDS multi-AZ + read replica** for prod
- [ ] **[DEFERRED FROM Phase 1] Valkey 3-node cluster** for prod
- [ ] **[DEFERRED FROM Phase 1] Kafka Schema Registry** deployed; producers wired
- [ ] **[DEFERRED FROM Phase 3] KEDA Prometheus-based scaler** for llms-gateway (and xAgent)
- [ ] **[DEFERRED FROM Phase 2] `auth.audit_log` monthly partitioning** + old-partition DETACH after 90 days
- [ ] **[DEFERRED FROM Phase 6] Async `last_accessed_at` tracker** — CONDITIONAL on hot-write contention in prod load test
- [ ] **[DEFERRED FROM Phase 4] Embedded-library guardrails mode** — CONDITIONAL (land if guardrails RTT > 20% of xAgent task p50 in prod for 7 days)

**Domain 3 — Rate Limiting & Per-Tenant Quotas (AMENDED 2026-06 — single-owner rule, see Amendment Log: Phase 13 tunes values + adds anomaly detection only; it owns no canonical tiers and no enforcement logic):**
- [ ] **Quota tier VALUES tuned** in `contracts/billing/tiers.yaml` (canonical home: Phase 0 contracts repo) from load-test results — every service already reads it via Auth `/v1/tenants/{id}/limits`
- [ ] Rate limits tuned per service based on load test results
- [ ] 429 response format verified (Retry-After header per Contract 9)
- ([DEFERRED FROM Phase 5/6] quota items DELETED 2026-06 — RAG storage quotas and memory row quotas are ⚡ first-cycle ENFORCEMENT in Phases 5/6; Phase 13 only tunes their limit values + adds the 80% operator alert. See Amendment Log)
- [ ] **[DEFERRED FROM Phase 7] Per-tenant tool ACL** (`registry.tenant_tool_acl`); free-tier tenants can't invoke enterprise-only tools
- [ ] **[DEFERRED FROM Phase 11] Per-tenant z-score cost anomaly** after 2-week prod observation period

**Domain 4 — Public API Documentation:**
- [ ] OpenAPI specs validated and complete for all services (all Phase 0–12 post-edit contracts reflected)
- [ ] Documentation site live at `docs.cypherx.ai` (same Route53 zone + ACM cert as Phase 1 Component 5)
- [ ] Quickstart guide written and tested by someone unfamiliar with the platform
- [ ] Webhook/Kafka event schemas documented (matches Contract 5 envelope + Phase 3/4/9/10/11 event payloads)
- [ ] API playground working (staging env keys; auto-rotated daily; rate-limited far below pro tier)
- [ ] **No `tenant_id` in any public endpoint parameter** — derived from JWT always (cross-tenant API spec validation)
- [ ] **[DEFERRED FROM Phase 9 + Phase 12] SSE multiplexed task feed** lands — Phase 12 dashboard upgraded from long-poll

**Domain 5 — Sandbox & Marketplace:**
- [ ] **Sandbox in SEPARATE AWS account + SEPARATE EKS cluster** (`cypherx-ai-sandbox` + `cypherx-sandbox`) — namespace-only isolation is insufficient for external developers
- [ ] Sandbox API keys issued to developers; sandbox-only (not valid in prod)
- [ ] Sandbox rate limits: free tier ÷ 10
- [ ] Sandbox data auto-purged every 7 days via tenant-wipe flow
- [ ] Sandbox billing push to px0 disabled
- [ ] Agent marketplace v1 browsable (`auth.agents.marketplace_public` flag + fork endpoint)

**Domain 6 — Operational Readiness:**
- [ ] Per-service SLO docs published at `runbooks.cypherx.ai/slo/<service>`
- [ ] Error budget policy implemented; freeze non-critical deploys when budget consumed early
- [ ] **Status page automated** (Betterstack ← Alertmanager webhook); 4 components displayed (API, Dashboard, Documentation, Sandbox); auto-resolves after 5-min alert clear
- [ ] PagerDuty schedules from Phase 11; primary + secondary on-call; 15-min escalation
- [ ] Every alert has `runbook_url` annotation (CI-enforced per Phase 11 post-edit)
- [ ] Post-mortem template in repo; SEV-1/2 incidents get post-mortems within 5 business days
- [ ] RBAC for ops surfaces: status-page admin, ArgoCD prod sync approver (rotating, NOT PR author), Doppler prod-config writer

**Domain 7 — Service Identity Migration (SPIFFE):**
- [ ] **[DEFERRED FROM Phase 0 Contract 12] SPIRE server + agent deployed**; SPIFFE trust domain per env
- [ ] Auth `POST /v1/service-tokens` accepts both bootstrap-secret AND SPIFFE SVID
- [ ] **`auth.feature_flags` table created** (Phase 13 migration); seeded with `bootstrap_secret_enabled='true'`; Auth checks flag on every service-token mint (30s in-process cache)
- [ ] Per-service migration to SPIFFE — **IN-CLUSTER ONLY** (order: frontend-bff → tool-* → memory → rag → guardrails → llms → a2a-router → xagent → auth); **skills-ci and other GH-Actions workflows EXCLUDED** (no SPIRE agent on GH runners — they stay on the OIDC → Secrets Manager → bootstrap path established in Phase 8 / Phase 1 Component 18)
- [ ] Each migrated service runs 7 days on SPIFFE in prod before cutover
- [ ] **Bootstrap-secret path DISABLED for in-cluster services** via `auth.feature_flags('bootstrap_secret_enabled') = 'false'` (Phase 0 Contract 12 mandate); CI workflow secrets in `cypherx/ci/*` NOT affected
- [ ] Doppler `service-auth/<svc>/bootstrap_secret` rows for in-cluster services deleted (or moved to glacier for break-glass); `cypherx/ci/<workflow>/bootstrap_secret` in Secrets Manager retained on 90-day rotation
- [ ] **Per-target `aud` scoping enabled** — no more `aud=["*"]` (Phase 0 Contract 12)
- [ ] Penetration test: stolen in-cluster bootstrap secret rejected by Auth (post-flip)
- [ ] Contract 12 documentation updated; SPIFFE marked canonical for in-cluster, GH-OIDC marked canonical for external CI, bootstrap-secret in-cluster deprecated

**Domain 8 — Disaster Recovery & Business Continuity:**
- [ ] RTO/RPO targets documented (prod: 4h/15min; staging: 24h/1h)
- [ ] **DR runbook at `runbooks.cypherx.ai/dr`** — step-by-step for each scenario
- [ ] Q1 drill: RDS PITR restore + smoke test; meets RTO
- [ ] **Q2 drill: full cluster rebuild from gitops + restored data** — meets RTO; this is the killer feature drill
- [ ] **Q3 drill: cross-region failover** — Aurora Global Database promotion (or DMS-replica failover per Option B); replication lag at failover ≤ 15min; Route53 TTLs ≤ 60s; meets 4h RTO
- [ ] Q4 drill: tenant-wipe + backup-restore interaction
- [ ] **Outbox replay drill** (simulated total Kafka loss) — every outbox row republished, no double-billing (Idempotency-Key verifies)
- [ ] **Continuous cross-region RDS replication** (Aurora Global Database PREFERRED; DMS Option B fallback) — daily snapshots are NOT sufficient for 15min RPO; if Phase 1 RDS is non-Aurora, upgrade is a Phase 13 prerequisite
- [ ] DR-region snapshot copy (secondary backup, daily, 30-day retention) — insurance only; not the primary RPO mechanism
- [ ] S3 cross-region replication for `cypherx-rag-<env>` + `cypherx-billing-output` (prod only)
- [ ] MSK MirrorMaker 2 to DR region for prod (last 30 days delete-policy + all compact-policy)
- [ ] Multi-region KMS keys for prod (cypherx-auth-signing, cypherx-rag, cypherx-byok, cypherx-billing-output)
- [ ] Doppler daily export to encrypted S3 (insurance against Doppler outage)
- [ ] DR drill cadence — quarterly prod, monthly staging — calendared and runbook reviewed after each
- [ ] **`terraform/modules/dr/`** module provisioning DR-region topology (Global Database secondary, MirrorMaker, CRR, multi-region KMS, pre-provisioned EKS cluster shell)

**Domain 9 — Compliance Readiness:**
- [ ] SOC 2 Type 1 controls mapping (every TSC → existing platform feature / runbook / policy)
- [ ] SOC 2 audit period started (≥ 3 months of evidence collection before audit start)
- [ ] GDPR DPIA completed
- [ ] Data-flow map documented (every PII path: Phase 4 redaction, Phase 6 user-scope, Phase 5 RAG content)
- [ ] **DSAR aggregator** — `POST /v1/admin/dsar` on platform-mgmt; Kafka fan-out via `cypherx.tenant.dsar.requested`; per-service consumers write JSON-Lines to S3 `cypherx-dsar-output-<env>/<request_id>/`; aggregator builds manifest.zip + pre-signed URL; 25-day SLA sweeper alerts before 30-day GDPR deadline
- [ ] **`platform.dsar_requests` table** + DSAR Kafka topic + DLQ provisioned via Phase 13 migration
- [ ] Per-service DSAR consumers shipped (Memory, RAG, xAgent, Auth, Billing) — each emits `cypherx.<service>.dsar.exported`
- [ ] DPA agreements signed with all sub-processors (Anthropic, OpenAI, AWS, Doppler, px0, Betterstack, PagerDuty, GitHub)
- [ ] **Customer-facing audit-log export API** (`GET /v1/admin/audit/export` with date range + tenant filter → JSON-Lines to S3 pre-signed URL); requires `audit:export` scope registered by Phase 13 migration
- [ ] **Per-tenant data residency advertisement** (v1: documentation-only — all tenants in us-east-1; visible in tenant dashboard; full per-tenant region routing is 📋 post-Phase 13)
- [ ] **Configurable per-tenant data retention** (override platform defaults; implemented via per-service tenant_config tables — Phase 5/6 already have the column shape)
- [ ] ISO 27001 SoA (optional — many enterprise tenants require)

**Cross-phase resources (Phase 13 ownership):**
- [ ] **`platform-migrations/phase-13/`** — `auth.feature_flags`, `registry.tenant_tool_acl`, `platform.dsar_requests`, audit/dsar scope registration; CODEOWNERS = Auth + Platform + Security; pre-install K8s Job; idempotent
- [ ] **`terraform/modules/sandbox/`** — separate AWS account + VPC + EKS + RDS/MSK/Valkey + cross-account ECR pull + Route53 zone + KMS keys for `cypherx-ai-sandbox`
- [ ] **`terraform/modules/dr/`** — Aurora Global Database secondary (or DMS Option B), MirrorMaker, CRR, multi-region KMS, pre-provisioned EKS shell in DR region
- [ ] **`terraform/modules/frontend/` reused** for `docs.<env>.cypherx.ai` (Domain 4) with bucket `cypherx-docs-<env>` + `CypherX-DocsDeployerRole` per env
- [ ] **`contracts/billing/tiers.yaml`** populated (canonical quota table; every rate-limiter reads via Auth `/v1/tenants/{id}/limits`)
- [ ] **`contracts/sre/slo.yaml`** populated (per-service SLO targets; Prometheus recording rules generated from it)

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Phase 13 Carries Too Much Critical Infrastructure — REAL
Evidence: lines 17–37 ("Domains 7, 8, 9 absorb items deferred to Phase 13"). 15+ production-critical deferrals (CloudTrail, WAF, SPIFFE, per-tenant quotas, KEDA, `auth.audit_log` partitioning, DR/backup).
**Mitigation:** reclassify Domain 1 (pen-test, OWASP, WAF) and Domain 2 (load-test SLO) as Phase-1/0 pre-MVP; treat Domains 7/8/9 (SPIFFE, DR, SOC2/DPIA) as Phase-14 post-MVP. ~40 % scope reduction for first-cycle.

### 2. Over-Centralization of the Control Plane — PARTIAL
Evidence: lines 711–739, 452 (platform-mgmt is DSAR + incident + audit fan-out).
**Mitigation:** distribute Betterstack incident creation to per-service alerting rules (not platform-mgmt POST). DSAR aggregator is unavoidable but treat platform-mgmt availability as Phase-13 SLO blocker.

### 3. Excessive Dependence on Event-Driven Coordination — REAL
Evidence: lines 61, 228–230, 620–622, 665–672. No documented consistency window per service.
**Mitigation:** add `cypherx.<service>.consistency_window_secs` config; cross-service tests must verify idempotency under network partition > window. In-flight events on delete-policy topics may be lost if Kafka unrecoverable before MirrorMaker catch-up — document this as known limitation.

### 4. Platform Engineering Complexity vs First-Cycle Product Needs — REAL
Evidence: lines 2, 18, 42 (9 domains, 2 Terraform modules, 4 migrations, 10+ deferrals).
**Mitigation:** first-cycle triage — Domain 1 + Domain 2 REQUIRED; Domain 7 (SPIFFE), Domain 8 (cross-region DR), Domain 9 (SOC2 + DPIA + ISO) DEFER to Phase 14. Bootstrap-secret + same-region PITR sufficient until enterprise customers arrive.

### 5. xAgent Risks Becoming a Full Workflow Engine — PARTIAL
Evidence: lines 129, 337–338 (xAgent owns Tasks API + Workflows API; A2A is separate).
**Mitigation:** annotate in API surface — `Tasks API` and `Workflows API` are user-submitted only, NOT service-orchestration. Service-to-service orchestration lives in Phase 10 A2A; no overlap if separation is enforced in code review.

### 6. Compliance and Export Systems May Cause Storage Explosion — REAL
Evidence: lines 767–768, 788–789 (DSAR topic 30 d; no S3 bucket lifecycle; audit export retention unstated).
**Mitigation:** (a) S3 `cypherx-dsar-output-<env>` lifecycle — delete `<request_id>/*` 90 d after completion; (b) `auth.audit_log` partitions detach to S3 cold storage after 90 d, Glacier transition at 1 y, delete after retention period; (c) per-tenant DSAR rate limit (e.g., 10 active requests/tenant).

### 7. Strong Security Consistency Across All Phases — VERIFIED
Evidence: lines 55–63, 84, 146–149, 689–690, 895–914. Systematic cross-tenant denial, secret redaction, RLS without BYPASSRLS, mTLS+TLS, audit redaction.
