# CLAUDE.md — archive (Manoj specs)

> The CypherX AI platform's **planning / specification repo**: the master plan, the enterprise/infra flow, the per-service stack policy, and the 15 authoritative per-phase build specs (with their audit + execution-plan + binding amendments). **No code lives here** — it is the design source future sessions cross-reference. Root platform guide: [../CLAUDE.md](../CLAUDE.md).

## What this is

This repo holds **`archive/Manoj/`** — the authoritative platform plan and the per-phase build specs for the whole CypherX AI platform. Everything load-bearing is Markdown (plus one JSON). It is the "design once, then build" repo: services are implemented in *other* repos against these specs and against the `contracts/` repo. It is **complete as a spec repo** (its deliverable is the documents, all present); it intentionally ships **zero runnable code, no Dockerfile, no build manifests**. The platform itself is at phase status ⏳ *pending / partial* per the phases index (the agent spine — auth→xagent→guardrails→llms — is the only verified-working part).

> The root `README.md` is the **default GitLab template** (boilerplate "Add your files / Getting started"). Ignore it — it carries no project information. All real content is under `Manoj/`.

## Tech stack

- **Markdown** for all specs; **one JSON** file (`Manoj/phases/amendments/plan-fixes.json`).
- No language, framework, build tool, runtime, package manager, tests, or DB.
- **Git is the versioning + audit trail.** Branches: `main`, `development` (current local HEAD, ahead of `main`), and `saturday` on origin (`gitlab.com/cypherx-ai/archive`). The root `CLAUDE.md` is currently untracked.

## Repository layout

| Path | What it holds |
|------|---------------|
| `README.md` | Default GitLab template — **ignore**. |
| `Manoj/CYPHERX_AI_PLATFORM_PLAN.md` | Master platform plan (v1.0): vision, system architecture, repo map, component plans (px0, Auth, LLMs, Guardrails, Memory, RAG, Tools, Skills, xAgent, Platform, Frontend, SDKs), cross-cutting concerns, build order, design principles. A2A + MCP are the two core protocols. |
| `Manoj/CYPHERX_AI_ENTERPRISE_FLOW.md` | Enterprise/infra architecture flow (v1.0): the cloud target — AWS/EKS, Kong, Istio, Kafka(MSK), Postgres(RDS+PgBouncer), Valkey, Prom/Grafana/Loki/Tempo, Doppler→Vault, Terraform/Terragrunt, GitHub Actions+ArgoCD, namespaces. **Cloud-target view — the local first-cycle runtime differs (see Gotchas).** |
| `Manoj/stack.md` | **Per-service stack policy** (v1.1): Kotlin+Spring+Gradle for glue/CRUD (auth, platform, px0-bridge); Python+FastAPI for AI services + agent runtime (llms, guardrails, memory, rag, agent-runtime, orchestrator, a2a-router); Next.js for UI; **any stack** for MCP tools; standard project structure per stack; **auth-service authenticates AGENTS, not users** (px0 owns user auth). Atlas for all migrations. |
| `Manoj/phases/README.md` | **Master phase index — start here.** Phase table 0–14, First-Cycle path, build order, legend (⚡ first-cycle / 📋 enterprise / 🏗️ plan-separately / ✅🔄⏳ status). Carries the Contract-15 gating amendment. |
| `Manoj/phases/phase-00-contracts.md` | **Most important spec.** Defines Contracts 1–21 (JWT, error format, A2A, MCP manifest, Kafka envelope, log format, health/metrics, trace headers, versioning/pagination/idempotency, OpenAPI base, skill schema, service-to-service token, tenant model, migrations, smoke test, step-up token, behavioral policy, API-key/ACL, usage metering, onboarding, webhooks). The `contracts/` repo is the live source of truth for these. |
| `Manoj/phases/phase-01..14-*.md` | Per-component build specs: 01 infra, 02 auth, 03 llms, 04 guardrails, 05 rag, 06 memory, 07 tools, 08 skills, 09 xagent, 10 a2a-orchestration, 11 platform-mgmt, 12 frontend, 13 hardening, 14 sdks. Each = Overview → HLD (system context + diagrams) → LLD (data models, APIs, K8s) → First-Cycle + Full-Enterprise checklists. |
| `Manoj/phases/AUDIT_REPORT.md` | Two applied audit rounds (2026-05-23, Opus 4.7): Round 1 internal first-cycle hardening + Round 2 external-operability. Lists each fix and which phase doc it touched. |
| `Manoj/phases/EXECUTION_PLAN_FIRST_CYCLE_100.md` | **The active backlog.** 14 dependency-ordered work packages WP01–WP14 to reach "100% of first cycle", with per-WP scope, tests, the "Explicitly excluded" list, and execution risks. |
| `Manoj/phases/amendments/plan-fixes.json` | **Binding.** 26 decided plan corrections (9 BLOCKERs). **These OVERRIDE the phase docs where they conflict.** Read this before trusting any phase-doc detail it amends. |

## Build, test, run

**None.** Nothing here compiles, runs, or is tested. There is no Dockerfile and this repo is **not** a service in `infra/compose/docker-compose.yml` (no port, no `/livez`/`/readyz`).

To **use** the repo: read the docs. Entry point: `Manoj/phases/README.md` → the phase index → the specific phase doc owning the component you are working on. When documenting another repo, find its owning phase here (e.g. auth → `phase-02-auth.md`, llms → `phase-03-llms.md`, guardrails → `phase-04-guardrails.md`, xagent → `phase-09-xagent.md`, tools → `phase-07-tools.md`). Then cross-check `amendments/plan-fixes.json` for any binding override.

## Configuration & secrets

None. No env vars, no Doppler usage, no `.env.example`, no mock toggles — this is a static document set. (The platform-wide toggles `MOCK_PROVIDERS`, `MOCK_EMBEDDINGS`, `SEARCH_PROVIDER`, `CLASSIFIER_MODE` are *described* in these specs but consumed by the service repos, not by this one.)

## Contracts & cross-repo dependencies

- This repo **produces specs**; it consumes nothing at runtime.
- The phase docs (esp. `phase-00-contracts.md`) **define** Contracts 1–21. The separate **`contracts/` repo is the single source of truth** for the live cross-service agreements; these docs are the design rationale + per-service application. If a contract detail here conflicts with `contracts/`, **`contracts/` wins** for implementation.
- Per-service ownership the specs assign (useful when reading other repos): Auth issues **agent** JWTs (Contract 1) + **service tokens** (Contract 12) and takes interim ownership of the tenant `plan` column; each service owns its own Postgres schema/role with RLS (`SET LOCAL app.tenant_id` per txn); Kafka topics follow the Contract 5 envelope and `cypherx.<service>.<event>` naming (e.g. `cypherx.tenant.*`, `cypherx.llms.usage.recorded`, `cypherx.tools.invocation.metered` emitted by xAgent); `tenant_id == px0.org_id` except the platform-tenant UUID `00000000-0000-0000-0000-000000000001` (Contract 13).

## Invariants & guards (do NOT break)

- **Do NOT add code, Dockerfiles, or build files here.** This is a docs/spec repo by design; keep it that way.
- **`amendments/plan-fixes.json` is authoritative and OVERRIDES the phase docs** wherever they conflict. Never "fix" a phase doc to disagree with a decided amendment. There are 26 amendments (9 blockers): jti replay-window rescope, AWS-only BYOK → pluggable `sealed:v1`/`env:` backends, the `request_id`→`llm_call_id` billing-key fix, guardrails policy-CRUD promotion, xAgent Contract-15/idempotency hole + agent-id mis-attribution live bug, px0-only frontend login → platform-credential login, the `cypherx.memory.write.requested` topic deletion (direct HTTP instead), and the RAG/Memory→absent-LLMs-embeddings dependency.
- **Treat the specs as immutable agreements unless explicitly amending.** Per phase-00: contracts are "written once, versioned forever" — deprecate, never silently rewrite; breaking changes bump the version.
- **First-cycle scope is deliberately narrow:** spine = Phases 0–4 + 9A only; **no tools, memory, skills, RAG, A2A, or orchestration in first cycle.** Definition of done = Contract-15 smoke test, cases **1–10** gate the spine, **11–15** gate the enterprise wave (WP12/WP14); **all 15** for full sign-off. Do not re-introduce the superseded "all 10 cases" wording.
- **auth-service authenticates AGENTS, not end users** (px0 owns user auth). Two independent JWT systems — different issuers/JWKS/audiences. Don't conflate them.
- These docs describe an **AWS/K8s cloud target**, but the actual first-cycle runtime is **compose + Neon Postgres + Valkey + Redpanda + MinIO** with compose-parity equivalents. The execution plan / amendments are the reconciled truth where the enterprise-flow doc names cloud-only mechanisms (Kong, Istio, IRSA, Doppler, Secrets Manager).

## Gotchas & current status

- **Root `README.md` is boilerplate** — never cite it as project intent; use `Manoj/`.
- **`CYPHERX_AI_ENTERPRISE_FLOW.md` and many phase docs assume AWS/EKS/Kong/Istio/Doppler** which the real local/first-cycle runtime does NOT use. The `AUDIT_REPORT.md` (Round 2) and `plan-fixes.json` add "compose-parity" reconciliations; trust those over the raw cloud-target prose.
- **The platform is at planning/early-build status** (phases index = ⏳/partial). Don't assume any spec section is implemented — verify against the actual service repo on disk. Only the four-service spine is verified working.
- **The execution plan flags real risks** (Neon latency vs guardrails 30/50ms input + 60/100ms output SLOs, the `llm_call_id` billing-key migration on the live revenue-bearing spine, detoxify/torch ~2GB packaging + slim/ml image split, WP06 embeddings as the single-threaded critical path gating RAG/Memory/xAgent-enhanced) — surface these when working the affected service.
- **Skills (Phase 8) is excluded from first cycle entirely**; xAgent's `SKILL_LOAD` stage stays disabled. Many Auth/LLMs/Guardrails/xAgent/RAG/Memory/Tools/Frontend enterprise items plus all deploy-target machinery (K8s/Kong/Istio/Argo/Terraform/Doppler) are explicitly deferred — read the "Explicitly excluded" list in the execution plan before assuming a feature is in scope.
