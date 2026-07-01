# CLAUDE.md — platform

> CypherX **control plane** repo (intended `platform-service`, namespace `platform-mgmt`) — manages/monitors/governs every other service. It does NOT run agents. **Currently an empty GitLab template stub**; everything below the "What this is" line (except the layout/build facts) is INTENDED design from the Phase 11 spec, not code on disk. Root platform guide: ../CLAUDE.md. Contracts source of truth: ../contracts/. Build spec: ../archive/Manoj/phases/phase-11-platform-management.md.

## What this is
This repo is meant to become the CypherX platform-management service — the control plane that aggregates observability, manages config & deployments, rolls up costs, publishes billing to px0, and governs cross-service quotas. It implements **Phase 11** (`../archive/Manoj/phases/phase-11-platform-management.md`); `../archive/Manoj/stack.md` (line 42) names the service `platform-service`.

**Implementation status: STUB / EMPTY.** On disk there is only the default GitLab `README.md` (boilerplate "Getting started" template text) and `.git`. All branches (`development`, `main`, `feature/base-implementation`, `origin/development`) hold the identical single file under one "Initial commit". No source, build files, Dockerfile, migrations, tests, config, or real docs exist. Phase 11 is explicitly "📋 Not required for first cycle — begin after core services are operational."

## Tech stack
- **On disk now:** none (Markdown README only).
- **Intended** (`../archive/Manoj/stack.md` lines 42 & 236): **Kotlin + Spring Boot + Gradle (KTS)** — same stack as px0. Decision rule (stack.md line 98/236): it is CRUD + Kafka consumers + px0 DTO sharing and does NOT serve ML/LLM workloads, so Kotlin (not the Python/FastAPI used by AI services).
- Backing infra (intended): PostgreSQL `platform.*` schema (Neon, external), Kafka (Redpanda locally), Valkey (hot quota counters). Canonical in-container port **8080**.

## Repository layout
```
platform/
  README.md     ← default GitLab template boilerplate (only tracked file)
  CLAUDE.md     ← this file (untracked)
  .git/
```
Nothing else exists. When scaffolded as a Spring Boot service, expect the standard `build.gradle.kts`, `settings.gradle.kts`, `src/main/kotlin`, `src/main/resources` (incl. `logback-spring.xml` for Contract 6 JSON logs), `src/test/kotlin`, `Dockerfile`, and Flyway/Liquibase migrations creating the `platform` schema (per stack.md Kotlin scaffold, lines 125-137).

## Build, test, run
No build/run is possible today (no manifests). **Intended** once scaffolded:
- Host: `./gradlew build`, `./gradlew test`, `./gradlew bootRun`.
- Docker / infra/compose: built like the other Kotlin services; in-container port **8080**. (No `platform-service` block exists in `../infra/compose/docker-compose.yml` yet — verified: all "platform" matches there are the `AUTH_PLATFORM_AUDIENCE` env var / prose, not a service.)
- Health (Contract 7 — `../contracts/health/`): `/livez` (process-only, never touches DB/Kafka/px0/Slack), `/readyz` (hard deps: Postgres + Kafka; px0/PagerDuty/Slack soft), `/metrics`. K8s adds a startup probe (~60s grace, `failureThreshold: 12 @ 5s`) on `/readyz`.

## Configuration & secrets
Postgres is EXTERNAL (Neon); no postgres container. Secrets live in **Doppler**; only `.env.example` is ever committed. Intended env vars (Phase 11 K8s spec):
- `DATABASE_URL` — PgBouncer → `platform` schema (runtime user `plat_user`).
- `KAFKA_BROKERS`, `KAFKA_SASL_PASSWORD` — service is event-driven.
- `AUTH_SERVICE_URL`, `AUTH_JWKS_URL` — auth-service in `shared-core`; JWKS verified in-cluster only, 5-min cache, refresh-on-`kid`-miss rate-limited 1/min.
- `SERVICE_BOOTSTRAP_SECRET` — Contract 12 service auth (`service-auth/platform-mgmt/bootstrap_secret`).
- `ARGOCD_API_URL`, `ARGOCD_WEBHOOK_HMAC_KEY` — registry/deploy sync + webhook receiver.
- `PX0_API_URL`, `PX0_API_KEY` — billing push.
- `PAGERDUTY_SERVICE_KEY`, `SLACK_WEBHOOK_{PLATFORM,XAGENT,BILLING}` — alert routing.
- `GITHUB_APP_PRIVATE_KEY` — rollback via cypherx-gitops-bot GitHub App.
Local platform default is keyless (`MOCK_PROVIDERS=true`, etc.), but this service has no LLM/provider calls of its own — its mocks would be px0/Slack/PagerDuty/ArgoCD stand-ins, to be defined when built.

## Contracts & cross-repo dependencies
Source of truth = `../contracts/`. Intended usage:
- **Consumes contracts:** JWT (`jwt/`), Kafka envelope (`kafka/`), error format (`api/`), log format (`logging/`), health (`health/`), tenant/RLS (`tenant/`), service-auth (`service-auth/`), usage/billing/webhooks.
- **Calls:** auth-service (`internal:read/write` — tenant/agent admin, audit reads), llms-gateway (`internal:read` — model list for cost calc), px0 billing API, ArgoCD API, GitHub App.
- **Kafka consumed:** `cypherx.llms.request.completed`, `cypherx.billing.usage.recorded`, `cypherx.*.usage.recorded` (quota agg); the separate px0-bridge consumes foreign-prefix `px0.org.created|suspended|deleted`.
- **Kafka produced (CypherX-owned, provisioned via Phase 11 Terraform — never auto-created):** `cypherx.platform.config.updated` (+DLQ), `cypherx.tenant.wipe.requested` (+DLQ), `cypherx.tenant.quota.breached`.
- **DB owned:** PostgreSQL `platform` schema — `services`, `config` (+`config_current` view), `outbox`, `tenant_costs`, `billing_push_log`, `deployments`, `tenant_running_totals`, `px0_org_log`.

## Invariants & guards (do NOT break)
From the Phase 11 spec (apply when implementing; none enforced by code yet):
- **Service registry is a derived/cached view, never authoritative.** K8s API (EndpointSlices) owns liveness; ArgoCD owns deployed version. No external write API — mutations only via sync jobs. `depends_on` is display-only; the enforceable graph is `auth.service_acl`.
- **Config is append-only versioned** (`UNIQUE (service, environment, key, version)`). PUT inserts a new version, never mutates prior — otherwise `/history` lies.
- **Config write + Kafka event are transactional via `platform.outbox`** (DB write and event must not diverge).
- **`tenant_costs.total_cost_usd` is a generated column** — never maintain it manually.
- **`tenant_costs` and `tenant_running_totals` are tenant-scoped → RLS required** (Contract 13); cross-tenant reads only via the platform-admin DB role.
- **px0 billing push is idempotent** via `Idempotency-Key: billing-<tenant>-<period>`; required, not optional (prevents double-billing on retry).
- **Rollback is `platform:admin` only**, rate-limited 1 per (service, env) per 5 min; reverts via gitops-bot PR; audit-logged. No fine-grained alternative (high blast radius).
- **ArgoCD webhook (`/v1/internal/argocd-webhook`) uses HMAC, not service-JWT** (ArgoCD doesn't speak Contract 12); internal-only, Istio-restricted to the `argocd` namespace.
- **Kafka topic auto-creation is forbidden** (Phase 1) — all topics provisioned via Terraform.
- **`/livez` must be process-only** — never touch DB/Kafka/px0/Slack.
- **px0.* topics are owned by px0** — CypherX never creates them.

## Gotchas & current status
- This repo is a **stub**: the `README.md` is unmodified GitLab boilerplate ("Add your files", "Editing this README", template suggestions) — it carries NO real project info. Do not treat its text as design; design lives in the Phase 11 spec + contracts.
- All Phase 11 components are 📋 (not started); none required for first cycle. There is no architecture-planning doc here yet — the spec itself says the internal architecture (config propagation, deployment orchestration, billing pipeline) "must be planned separately before implementation."
- Risk addenda in the phase spec (§"Audit Addenda", 2026-05-25) flag this becoming a "god service": consider extracting `quota-service` and `billing-aggregator`, adding billing idempotency (`event_id` + unique constraint on `tenant_costs`), config-event versioning/ACK, a fuller rollback state machine, and a DR/cold-start runbook — read that section before designing.
- A separate **`px0-bridge`** service (own namespace `px0-bridge`, also Kotlin per stack.md line 43) is part of Phase 11 scope but is its own repo/deployment — not necessarily this codebase.
