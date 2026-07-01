# CLAUDE.md — infra

> CypherX **Phase-1 Infrastructure Foundation**: Terraform/Terragrunt IaC + Terraform-managed k8s Helm add-ons for AWS EKS (dev/staging/prod), **plus** the two canonical local dev stacks (`infra/compose` = full platform in Docker, `infra/dev/local` = Tilt+kind deps). Platform root guide: ../CLAUDE.md.

## What this is
Implements the Phase-1 component set from `archive/Manoj/phases/phase-01-infrastructure.md` (contracts cross-ref `phase-00-contracts.md`). It is **fully authored IaC** — not running infra: every `terragrunt apply` is human-gated and out-of-band, so nothing here creates AWS resources on its own. It also owns `infra/compose/docker-compose.yml`, the **single source of local full-stack orchestration** for the whole platform (the file the root guide refers to). Status: **implemented** — modules, environments, k8s-addons, both local stacks, CI workflows, and the Component-21 smoke test are all present and substantive.

## Tech stack
- **Terraform `>= 1.9`**, **AWS provider `~> 5.x`** (pinned per module in `versions.tf`); wraps official `terraform-aws-modules` where sensible.
- **Terragrunt** drives `environments/` (remote S3+DynamoDB state + a generated AWS provider block).
- **Helm 3** add-ons as Terraform modules (`helm ~> 2.13`, `kubernetes ~> 2.31`, `kubectl(gavinbunney) ~> 1.14`); `Mongey/kafka` provider for `kafka-topics`.
- **Docker Compose v2**, **Tilt + kind**, **Caddy 2** (local edge), POSIX `sh` / Bash scripts, **GitHub Actions** CI, a small **FastAPI** echo-service (smoke test).

## Repository layout
| Path | Holds |
|------|-------|
| `modules/` | TF wrapper modules: `vpc` (+`security_groups.tf`), `eks-cluster`, `postgresql`, `valkey`, `kafka`, `iam`, `ecr-repo`, `dns`, `s3-bucket`, `tfstate-backend`, `postgres-bootstrap`, `kafka-topics`, `doppler-bootstrap`. Each = `main.tf`/`variables.tf`/`outputs.tf`/`versions.tf`/`README.md`. |
| `environments/` | Terragrunt: root `terragrunt.hcl` (backend + provider gen), `_envcommon/*.hcl` (per-stack shared config), and `dev/`,`staging/`,`prod/` (per-stack `terragrunt.hcl` + `env.hcl` sizing). 11 stacks/env. |
| `k8s-addons/` | ~19 Terraform-managed Helm releases: istio, kong, argocd, cert-manager, aws-lbc, karpenter, external-dns, metrics-server, reloader, kube-prometheus-stack, loki, tempo, promtail, doppler-operator, namespaces, network-policies. |
| `compose/` | **Full-stack local Docker** (`docker-compose.yml` + `docker-compose.override.yml`, `migrate.sh`, `topics-init.sh`, `edge/Caddyfile`, `observability/`, `.env.example`). |
| `dev/local/` | Laptop **deps-only** stack: `docker-compose.yml` (postgres+pgvector / valkey / redpanda / minio), `Tiltfile`, `seed/` (postgres-init.sql, kafka-topics.sh, doppler.env.example). |
| `scripts/` | `infra-smoke-test.sh` (Component-21 gate, 10 assertions), `kafka-bootstrap-topics.sh`. |
| `smoketest/` | `echo-service/` (FastAPI), Helm wrapper (`Chart.yaml`+`values.yaml` → base chart `cypherx-service`), `k8s/` manifests. |
| `ci/`, `.github/workflows/` | CI model doc + `infra-ci.yml`, `reusable-service-ci.yml`, `example-caller-ci.yml`, `schema-validate.yml` (pointer stub). |
| Docs | `README.md`, `PHASE1_NOTES.md` (author/operator split + apply order), `MIGRATION_NEON.md`, `RUNBOOKS.md`. |

> Note: platform-level `charts/` and `gitops/` are **sibling repos at the workspace root**, NOT inside `infra/`.

## Build, test, run
**Local FULL stack (`infra/compose/`)** — run all commands from `infra/compose/` so `.env` is picked up:
```bash
cp .env.example .env          # fill the Neon DSNs marked <<< SET REAL NEON VALUE >>> + SESSION_KEK_BASE64
docker compose --profile migrate up migrate    # ONE-TIME: schema/role/RLS/seed vs Neon DIRECT endpoint
docker compose up -d --build                    # deps + topics-init + full platform + edge
docker compose --profile observability up -d    # optional: otel-collector/tempo/loki/prometheus/grafana
docker compose --profile demo up -d --build demo # optional legacy demo BFF
docker compose config -q                         # validate
```
Canonical: every **app** service listens on **8080 in-container**, addressed by compose DNS (`http://<svc>:8080`). Host maps: auth `8080`, xagent `8083`, llms-gateway `8085`, guardrails `8086`, rag `8087`, memory `8088`, tool-registry `8089`, tool-web-search `8091`, frontend-bff `8092`→8088, frontend-app `3000`, demo `8090`→8090. Backing deps (host): redpanda `9092`/`9644`/`8081`(SR)/`8082`, valkey `6379`, minio `9000`/`9001`. Observability (host): otel `4317`/`4318`, tempo `3200`, loki `3100`, prometheus `9091`→9090, grafana `3001`→3000. Single edge entrypoint = Caddy on host **:8000** (`/`→SPA, `/bff/*`→BFF, `/api/<svc>/*`→service). Health: app services `/livez` (process-only) + `/readyz` (503 until Neon reachable; cold-start tolerated); edge `/healthz`.

> `docker-compose.override.yml` (Doppler-driven run) only adds `VALKEY_URL` to `llms-gateway` (the base file omits it, so llms falls back to an unreachable in-code `localhost:6379` and its idempotency/rate-limit/token-revocation features fail-open). It loads automatically with a bare `docker compose ...` from `compose/`.

**Local DEPS-ONLY (`infra/dev/local/`)** — laptop substitutes, no AWS, includes a **postgres container** (unlike compose):
```bash
docker compose -f infra/dev/local/docker-compose.yml up -d   # postgres+pgvector, valkey, redpanda, minio
cd infra/dev/local && tilt up        # adds topic bootstrap; service blocks are GUARDED (skip until code exists)
tilt up -- --deps-only               # deps only
```

**IaC** (no apply ever runs in CI; operator-only, role-assumed):
```bash
cd infra/environments/dev/<stack> && terragrunt plan && terragrunt apply
cd infra/environments/dev && terragrunt run-all apply    # whole env, dependency-ordered
```
**Smoke gate (Component 21):** `scripts/infra-smoke-test.sh --env dev --runs 2` — 10 assertions, must pass **two consecutive runs** before Phase 1 is complete (requires `kubectl`+`jq`; uses `helm`/`logcli`/`kcat`/`aws` when present). `infra-ci.yml` runs `fmt -check -recursive`, per-module `validate`/`tflint`, and a read-only `terragrunt plan` on PR.

## Configuration & secrets
- **Postgres is EXTERNAL (Neon serverless)** in `infra/compose` — there is NO postgres container. Per-service DSNs come from `.env`: `AUTH_DATABASE_URL` (JDBC, `currentSchema=auth`, separate `AUTH_DB_USERNAME`/`AUTH_DB_PASSWORD` — NOT in the URL), `LLMS_/GUARDRAILS_/XAGENT_/RAG_/MEMORY_/TOOL_REGISTRY_DATABASE_URL` (libpq, `options=-c search_path=<schema>`), `MIGRATE_DATABASE_URL` (**DIRECT** endpoint, owner role, used only by the migrate job), `DEMO_DB_URL` (owner DSN, demo only). `sslmode=require` is mandatory on every Neon DSN. DB name assumed `cypherx_platform`.
- **Apps use the Neon POOLED endpoint** (transaction mode, RLS-compatible); **migrations use DIRECT** (session-level advisory locks). Don't swap them.
- Backing deps: `KAFKA_BROKERS=redpanda:29092`, `VALKEY_URL=redis://valkey:6379`, MinIO at `http://minio:9000` (creds `S3_ACCESS_KEY`/`S3_SECRET_KEY`, default `cypherxlocal`).
- **Keyless local defaults** (`.env.example`): `MOCK_PROVIDERS=true`, `MOCK_EMBEDDINGS=true` / `EMBEDDINGS_FALLBACK_TO_MOCK=true` / `EMBEDDINGS_MOCK_FALLBACK=true` (memory), `SEARCH_PROVIDER=mock`, `CLASSIFIER_MODE=stub`. Flip a toggle off + add the key to go live.
- Secrets: only `.env.example` (compose) / `seed/doppler.env.example` (dev/local) are committed — both contain LOCAL-ONLY throwaway values (`AUTH_LOCAL_MASTER_KEY_B64`, `AUTH_BOOTSTRAP_TOKEN`, per-service `SERVICE_BOOTSTRAP_SECRET_*`, `REDACTION_HMAC_KEY_PLATFORM`, `SESSION_KEK_BASE64`, MinIO creds). `.env`/`.env.*` are gitignored. **Real secrets live in Doppler**; CI reads bootstrap secrets from **AWS Secrets Manager `cypherx/ci/*`** via the GitHub OIDC role — never a Doppler token in GitHub, never long-lived AWS keys.

## Contracts & cross-repo dependencies
- **Consumes `contracts/`**: Contract 1 (JWT/OIDC: `iss` opaque vs per-env JWKS URL), Contract 5 (Kafka envelope; smoke test asserts required keys + `partition_key==tenant_id`), Contract 6 (logs), Contract 7 (`/livez`,`/readyz`,`/metrics`), Contract 8 (W3C trace context), Contract 9 (Valkey idempotency), Contract 13 (tenant/RLS via `SET LOCAL app.tenant_id`), Contract 14 (migrations), Contract 15 case 5 (edge 401 on missing session cookie).
- **Kafka topics** pre-created by `compose/topics-init.sh` / `dev/local/seed/kafka-topics.sh`: `cypherx.auth.{token.revoked,policy.changed,config.updated,audit.appended,agent.deactivated,agent.updated}`, `cypherx.tenant.{created,suspended,resumed,plan_changed,pending_deletion,deleted}`, `cypherx.llms.{request.completed,usage.recorded}`, `cypherx.guardrails.{violation.detected,usage.recorded,policy.changed}`, `cypherx.agent.{task.completed,task.failed,tools.invocation.metered}`, `cypherx.rag.{ingestion.requested,ingestion.completed,ingestion.failed,usage.recorded}`, `cypherx.memory.{stored,deleted,gdpr.wiped}`. The cloud `modules/kafka-topics` declares a partly different Contract-5 core set (e.g. `cypherx.auth.agent.{registered,deactivated}` compact, plus `.dlq` for every non-compact topic) — local pre-creation and cloud TF are deliberately separate.
- **Builds/orchestrates** sibling service repos: `Shared Core/{auth,llms,guardrails,rag,memory}`, `xAgent/ax-1`, `Tools/{tool-registry,tool-web-search}`, `frontend/{bff,app,demo}`. `migrate.sh` mounts each service's `db/migrations/` (order: auth→llms→guardrails→xagent→rag→memory→tool-registry; applies `*__init.sql` then `*__seed.sql`) and provisions runtime roles `auth_user/llms_user/grd_user/xagent_user/rag_user/mem_user/tool_user` (password + `search_path` via idempotent `ALTER ROLE`). Step 0 creates `pgcrypto` (required) + `vector` (best-effort).
- The Helm base chart `cypherx-service` and the GitOps app tree live in **separate root-level repos** (`charts/`, `gitops/`).

## Invariants & guards (do NOT break)
- **ALB→Kong is plaintext HTTP** inside the VPC (`sg-alb`→`sg-kong` on 8000); Kong→backend is Istio mTLS. Locally there is **no Istio/Kong** by design (Component 17c) — services talk direct via DNS; do not add mesh/gateway to the local stacks.
- **`/v1/agents/*`, `/v1/tokens/*`, `/v1/authorize`, `/v1/service-tokens` route to Auth, NOT xAgent.**
- **IAM role split**: `GitHubActionsRole` (OIDC, explicit `Deny iam:*`), `TerraformInfraRole`, `TerraformIAMRole`, `EKSNodeRole`, `AWSLoadBalancerControllerRole`. Neither Terraform role may modify itself, GitHubActionsRole, or any `protected=true` role. Changes under `environments/*/iam/` + `modules/iam/` need a CODEOWNERS second approver.
- **Managed node groups vs Karpenter are non-overlapping**: `system-nodes`+`observability` are managed NGs (the latter pinned, not consolidated); `core`/`agent`/`tools` are Karpenter. Never add a managed NG for a Karpenter role.
- **Compact `cypherx.auth.agent.*` topics key on `agent_id`**, not `tenant_id` (a tenant-keyed compact topic collapses to one record/tenant and loses prior agent state). Normal `delete` topics (incl. `cypherx.smoketest.event`) key on `tenant_id`. Compact topics get NO `.dlq`.
- **Loki labels are low-cardinality ONLY** — `tenant_id`/`agent_id`/`request_id`/`trace_id`/`span_id` are JSON fields (`| json`), never stream labels (OOM guard; smoke assertion 3 enforces).
- **EKS API is private-only**; CI uses in-VPC self-hosted runners + IRSA, never IP allow-listing. **`:latest` image tags are forbidden** (CI hard-fails; ECR repos are `IMMUTABLE`). Cloud Kafka common config: `min.insync.replicas=2`, `unclean.leader.election.enable=false`, `compression.type=lz4`.
- **`CREATEROLE` is cluster-wide, not schema-scoped** — mitigated (per-service DDL user + isolated Doppler secret), not solved.
- No `0.0.0.0/0` ingress on private data SGs; state bucket is versioned/SSE-KMS/public-blocked/TLS-enforced. `terraform fmt`-clean (2-space, aligned `=`); no hardcoded secrets.
- Auth in compose runs with `SPRING_PROFILES_ACTIVE=""` (NOT `local`) so the Neon DSN takes effect; `application-local.yaml` hardcodes localhost. Per-service bootstrap secrets are injected via `SPRING_APPLICATION_JSON` with lowercase keys (`cypherx.service-auth.bootstrap-secrets.<service>`) and must match each caller's `SERVICE_BOOTSTRAP_SECRET`.

## Gotchas & current status
- **No AWS resources exist** — this is committed IaC only. Standing up an env requires the operator steps in `PHASE1_NOTES.md` §2 (state bootstrap, NS delegation, one-time Doppler human bootstrap, populate Doppler paths, install gitops bot, in-VPC runners) and the canonical apply order in §3 (state→IAM→vpc→dns→eks/ecr/data→re-apply IAM for LBC IRSA→k8s-addons→bootstrap stacks→smoke test ×2).
- **Two different local stacks, easy to confuse**: `infra/compose` = full platform vs **external Neon** (no pg container); `infra/dev/local` = deps-only Tilt/kind **with** a pgvector container + `seed/postgres-init.sql` (schemas, `*_user`/`*_ddl` roles, example RLS table). Editing `postgres-init.sql` requires `down -v` to re-run (it only executes on a first/empty data dir).
- `infra/dev/local/Tiltfile` service blocks are **guarded** — they expect code under `<repo-root>/services/<name>/` (a Phase-2 path that does not exist), so `tilt up` brings up only deps+topics. The real services live under `Shared Core/`, `xAgent/`, `Tools/`, `frontend/` and are wired by `infra/compose` instead.
- Redpanda Schema Registry is on **8081**; `memory`'s default `EMBEDDINGS_BASE_URL` is `:8081` and is overridden to `http://llms-gateway:8080` in compose to avoid the collision. Schema Registry must be a single (non-dual) listener or redpanda crash-loops.
- OTel span export is **opt-in**: with `OTEL_EXPORTER_OTLP_ENDPOINT` empty it is a complete no-op; set it to `http://otel-collector:4317` under `--profile observability`. Prometheus scrapes `/metrics` regardless.
- `demo` (--profile demo) predates the 8080 rule and listens on **8090 in-container** by design; `frontend-bff` listens on **8088 in-container** (`BFF_PORT`), host-mapped 8092. `frontend-app` inlines `NEXT_PUBLIC_BFF_URL` at **build time**.
- `schema-validate.yml` is a pointer stub — real schema CI runs in the **contracts** repo; do not duplicate it here.
- `modules/eks-cluster`,`postgresql`,`valkey`,`kafka` and several add-ons note "owned by other groups" — multiple authoring groups contributed; READMEs flag ownership.
