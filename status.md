# CypherX AI Platform — Implementation Status Report

> **Purpose.** A complete, evidence-backed end-to-end assessment of *how much of each service is actually implemented* versus what the planning specs (`archive/Manoj/`) call for, plus a phased roadmap of *what remains for future phases*. Written for engineering and product management.
>
> **Date:** 2026-07-09 · **Repo:** `agentic/` monorepo (`main` branch) · **Method:** three parallel source-code audits (Shared Core; agent-runtime/tools/skills/platform; frontend/contracts/infra) cross-checked against the phase specs, the root `END_TO_END_WALKTHROUGH.md`, and the `E2E_TEST_REPORT_2026-06-14.md`.
>
> **The one thing to know up front:** the phase index (`archive/Manoj/phases/README.md`) marks almost every phase `⏳ pending`. **That is stale and badly understates reality.** The code is far more complete than the index implies. This report follows the *code*.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Methodology & How to Read This](#2-methodology--how-to-read-this)
3. [Platform Overview](#3-platform-overview)
4. [Master Maturity Scorecard](#4-master-maturity-scorecard)
5. [Per-Service Deep Dives](#5-per-service-deep-dives)
6. [First-Cycle Definition of Done — Contract-15](#6-first-cycle-definition-of-done--contract-15)
7. [What's Next — Phased Roadmap](#7-whats-next--phased-roadmap)
8. [Cross-Cutting Risks & Known Gaps](#8-cross-cutting-risks--known-gaps)
9. [Appendices](#9-appendices)

---

## 1. Executive Summary

CypherX AI is a **multi-tenant, language-agnostic agentic platform** built as a set of independently deployable services glued by two protocols — **A2A** (agent-to-agent task delegation) and **MCP** (tool/skill invocation). It carries a general-purpose agent runtime + Console, plus a flagship product (`cypherx-a1`, an engineering-memory copilot), on a shared spine of Auth, LLMs, Guardrails, RAG and Memory.

**Overall verdict: the platform is well past "first cycle."** The proven four-service spine (Auth → xAgent → Guardrails → LLMs) is real and green — the canonical Contract-15 end-to-end suite passes **59/59** (2026-06-14). Beyond the spine, five Shared Core services, the agent runtime, both registries, two products, and the full Console frontend are all **genuine, tested implementations** — not scaffolds.

**What is solidly built and tested today:**
Auth, LLMs Gateway, Guardrails, Memory, RAG, xAgent/ax-1 (incl. reliability layer + async/SSE + coded-but-gated enhancement stages), Tool Registry, the Flow-Tool-Bridge + Node-RED (incl. the public `web_search` flow-tool that replaced the retired `tool-web-search` service), Skill Registry, the Console (Next.js SPA + Fastify BFF + demo harness), cypherx-a1 + its MCP facade, the entire `contracts/` repo, the 27-service Docker-Compose runtime, the base Helm chart, and the Terraform module library (as code).

**The genuine greenfield gaps (0%–low):**

| Gap | Owning phase | State |
|---|---|---|
| **A2A router + Orchestrator** (`xAgent/ax-2`) | Phase 10 | **0% — empty placeholder** (single `CLAUDE.md`) |
| **Platform-management control plane** (`platform/`) | Phase 11 | **0% — GitLab boilerplate + `CLAUDE.md` only** |
| **Full Skills execution engine** (`Skills/skill-registry`) | Phase 8 | **~30% — catalogue-only re-scope**; no retriever/template/step-execution |
| **Phase-13 hardening** (WAF, load test, DR, SOC2…) | Phase 13 | **~5–10% — design-stage** |
| **Cloud deployment** (Terraform → EKS/RDS/MSK) | Phase 1 (deploy) | **~85–90% authored, 0% applied** |
| **SDKs** (Python/TS packages) | Phase 14 | **0% — spec only** |

**Bottom line for planning:** first-cycle scope is functionally complete across the board; the remaining work splits cleanly into (B) two big net-new services — A2A/orchestration and the control plane — plus a real Skills engine; (C) enterprise-tier deepening of each already-built service; and (D) production hardening + actually standing up the cloud + SDKs.

---

## 2. Methodology & How to Read This

- **Spec vs. code.** Planned scope comes from `archive/Manoj/phases/phase-0X-*.md` (each: Overview → HLD → LLD → *First-Cycle Checklist* + *Full-Enterprise Checklist*) and the reconciled `EXECUTION_PLAN_FIRST_CYCLE_100.md` (work packages WP01–WP14 + an "Explicitly excluded" list). **Actual state** comes from reading source, routers, pipeline code, migrations, tests, and Dockerfiles.
- **Completeness numbers** are two distinct measures: **(a) first-cycle subset** — is the ⚡ minimum present? — and **(b) % of the *full enterprise* spec**. A service can be "first-cycle complete" and still ~40–60% of full spec, because the enterprise checklist is deliberately large.
- **Mock defaults are not defects.** The stack boots keyless for local/CI by design: `MOCK_PROVIDERS=true`, `MOCK_EMBEDDINGS=true`, `CLASSIFIER_MODE=stub`, `SEARCH_PROVIDER=mock`, `RERANK_PROVIDER=local`, mock email/captcha in Auth. Real production seams (`detoxify`, `serpapi`/`brave`, KMS, `secretsmanager:`) exist *behind* those toggles.
- **Doc authority.** Where an in-tree `README.md` / `REPO_ANALYSIS_*.md` contradicts the source, the **source wins**. Several such docs are stale "stub-era" artifacts (called out in §8). Each service's `CLAUDE.md` is generally accurate; exceptions are flagged.
- **Status legend:** ✅ complete for its bar · 🟡 partial / flag-gated / mock-default · 🔴 stub / not started · 📋 designed, deferred to a later phase by plan.

---

## 3. Platform Overview

**Runtime reality:** the platform actually runs on **`infra/compose/docker-compose.yml`** — a **27-service** stack against **external Neon Postgres** (there is *no* local Postgres container), with **Redpanda** (Kafka), **Valkey** (cache), and **MinIO** (object storage) as local containers, all behind a single **Caddy edge on `:8000`**. This stack runs **far more than the 4-service spine** — RAG, Memory, both registries, MCP servers, and a reference agent are all wired in.

Every app service listens on **`8080` in-container** by convention; `/livez` is process-only, `/readyz` checks real dependencies. Host port map:

| Service | Host port | | Service | Host port |
|---|---|---|---|---|
| Edge (Caddy) | `:8000` | | tool-registry | `:8089` |
| auth | `:8080` | | demo (opt-in) | `:8090` |
| frontend-app | `:3000` | | *(8091 freed — tool-web-search removed)* | |
| xagent (ax-1) | `:8083` | | frontend-bff | `:8092` → 8088 |
| llms-gateway | `:8085` | | cypherx-a1 | `:8093` |
| guardrails | `:8086` | | mcp-eng-memory | `:8094` |
| rag | `:8087` | | skill-registry | `:8095` |
| memory | `:8088` | | | |

Backing infra ports: Redpanda `:9092`, Valkey `:6379`, MinIO `:9000`/`:9001`, Mailpit `:1025`/`:8025`. Observability profile (`--profile observability`): otel-collector `:4317/:4318`, Tempo `:3200`, Loki `:3100`, Prometheus `:9091`, Grafana `:3001`.

---

## 4. Master Maturity Scorecard

> Completeness = % of the **full enterprise** spec. "First-cycle" = is the minimum viable slice present and working.

| # | Component | Path | Stack | Owning phase | First-cycle | Full-spec % | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **Auth** | `Shared Core/auth` | Kotlin/Spring | P02 | ✅ complete (+later-phase extras) | **~60%** | Most mature service; missing the P13 hardening block |
| 2 | **LLMs Gateway** | `Shared Core/llms` | Python/FastAPI | P03 | ✅ complete | **~45%** | Spine + embeddings + rerank/classify; no multi-provider/routing/budgets |
| 3 | **Guardrails** | `Shared Core/guardrails` | Python/FastAPI | P04 | ✅ complete | **~50%** | Full first-cycle + additive PII/injection/groundedness seams |
| 4 | **RAG** | `Shared Core/rag` | Python/FastAPI | P05 | ✅ complete | **~50%** | First-cycle + hybrid/sparse/rerank; no source-type breadth/versioning |
| 5 | **Memory** | `Shared Core/memory` | Python/FastAPI | P06 | 🟡 functional, **API diverges from spec** | **~35%** | Works, but 2-value scope ≠ spec's 5-value contract |
| 6 | **xAgent / ax-1** | `xAgent/ax-1` | Python/FastAPI | P09 (9A) | ✅ ~100% (9A) | **~60%** | 9A done + reliability + async/SSE; enhancement stages coded, gated off |
| 7 | **xAgent / ax-2** | `xAgent/ax-2` | — (empty) | P10 | n/a (📋) | **0%** 🔴 | A2A router + orchestrator — **no code anywhere** |
| 8 | **Tool Registry** | `Tools/tool-registry` | Python/FastAPI | P07 C1 | ✅ core complete | **~65%** | Discovery/versioning/health/access done; no capability-resolver/quotas |
| 9 | **web_search (flow-tool)** | `Tools/tool-flow-bridge` | Node-RED flow | P07 C3 → Phase 5 | ✅ replaced | — | Bespoke `tool-web-search` **decommissioned/removed**; capability now a Public `web_search` flow-tool (server `mcp-web-search`) |
| 10 | **Skill Registry** | `Skills/skill-registry` | Python/FastAPI | P08 | n/a (📋) | **~30%** 🟡 | Real service but **re-scoped** to catalogue+access only |
| 11 | **cypherx-a1 (+mcp)** | `CoreProjects/cypherx-a1` | Python/FastAPI | *(product, no phase)* | ✅ MVP complete | **~45%** (own roadmap) | GitHub ingest + graph + hybrid copilot + MCP facade |
| 12 | **Frontend (app+bff+demo)** | `frontend/` | Next/Fastify/Py | P12 | 🟡 ~85% | **~40–45%** | Full Console + BFF trust boundary; enterprise/px0 half deferred |
| 13 | **Contracts** | `contracts/` | Ajv/JSON-Schema | P00 | ✅ ~100% | **~95–100%** ✅ | All 21 contracts + validator + CI — strongest area |
| 14 | **infra / compose** | `infra/compose` | Docker Compose | P01 | ✅ ~95% | *(is the runtime)* | 27-service production-grade local stack |
| 15 | **infra / cloud IaC** | `infra/{modules,environments,k8s-addons}` | Terraform | P01 (deploy) | n/a | **~85–90% authored, 0% applied** 🟡 | Comprehensive IaC; never stood up; smoke-gate unmet |
| 16 | **infra / hardening** | `infra/` | — | P13 | n/a | **~5–10%** 🔴 | A few CI/security primitives; substance is future |
| 17 | **charts** | `charts/` | Helm 3 | P01 | ✅ ~90% | **~90–95%** | Base chart complete; unpublished (no OCI/lockfile) |
| 18 | **gitops** | `gitops/` | ArgoCD | P01 | ✅ scaffold | **~0% app population** 🟡 | 3 roots; **zero service child-apps** |
| 19 | **Platform (control plane)** | `platform/` | — (stub) | P11 | n/a (📋) | **0%** 🔴 | GitLab boilerplate + `CLAUDE.md` only |
| 20 | **SDKs** | *(none)* | — | P14 | n/a | **0%** 🔴 | Spec only; deferred until APIs stabilize |

---

## 5. Per-Service Deep Dives

### 5.1 Shared Core

#### Auth — `Shared Core/auth` (Phase 02) — ~60%
**Footprint:** Kotlin 2.0.21 / Spring Boot 3.3.5 (Gradle KTS). **111 source files**, 30 `@Test` across 10 Testcontainers suites, **10 SQL migrations** (Atlas), Dockerfile, `openapi.yaml`, 20 REST controllers. Largest, most mature service.

**Implemented:** Essentially the entire first-cycle surface, plus later-phase items pulled forward:
- Agent lifecycle CRUD + pagination + deactivate (`AgentController.kt`); API keys CRUD/rotate (`ApiKeyController.kt`); JWT mint (`TokenController.kt`); service tokens (`ServiceTokenController.kt`); OAuth2 `client_credentials` (`OAuthController.kt`).
- JWKS/OIDC discovery (`WellKnownController.kt`); envelope-encrypted signing keys + rotation (`signing/`, `crypto/`, `SigningKeyAdminController.kt`); `POST /v1/authorize` RBAC (`AuthorizeController.kt`).
- Tenant lifecycle create/suspend/resume/soft-delete (`TenantAdminController.kt`); quotas (`QuotaController.kt`); usage (`UsageController.kt`); onboarding signup/verify/resend (`OnboardingController.kt`); webhooks CRUD + delivery worker (`WebhookController.kt`, `WebhookDeliveryWorker.kt`).
- Live token revocation (`RevocationController.kt`, `RevocationChecker.kt`); tamper-evident audit + hash-chain verify + export + mirror (`AuditController.kt`, `AuditChainVerifyJob.kt`); transactional outbox + relay (`OutboxRelay.kt`); RLS (`TenantTx.kt`); rate-limit filter; secret-redaction logging.
- **Beyond first-cycle:** HIL step-up approvals (`HilController.kt`), orchestrator sub-agents (`OrchestratorController.kt`), and **end-user auth incl. Google OAuth** (`UserAuthController.kt`).

**Partial/mocked:** `GET /.well-known/jwks-signed.json` returns **503** (offline RSA-4096 root signer not provisioned — deliberate first-cycle stub); `auth.upstream_identity` ships empty → px0 `X-Px0-User-Token` verification disabled; behavioral-constraints engine is table + one shadow seed row (no enforcement); mock email/captcha defaults; audit-mirror/usage-rollup Kafka consumers OFF by default.

**Missing vs spec (mostly P13):** ABAC policy engine + `/v1/policies` REST API (repository exists, **no controller**); SPIFFE/workload-identity attestation; OPA bundle compilation; A2A delegation-chain validation; token binding (`cnf`) + single-use `jti` enforcement; federated IdPs; `audit_log` monthly partitioning.

---

#### LLMs Gateway — `Shared Core/llms` (Phase 03) — ~45%
**Footprint:** Python 3.12 / FastAPI (uv). **51 source files**, ~200 test functions / 27 files, **11 migrations** (Atlas), Dockerfile, `eval/` harness (NDCG/MRR).

**Implemented:** The full critical-path spine + WP05/WP06 + additive endpoints:
- `POST /v1/chat/completions` streaming + non-streaming with server-side tool-call aggregation + tool-emulation shim (`api/chat.py`, `services/tool_emulation.py`); `POST /v1/embeddings` (`api/embeddings.py`); `GET /v1/models|/v1/usage|/v1/cost` + alias CRUD (`api/read.py`).
- **Both provider adaptors:** `anthropic_provider.py`, `openai_provider.py` (+ `mock.py`); normalizer with tool_use↔tool_calls + cache-token mapping (`services/normalizer.py`).
- DB-authoritative aliases/pricing/capabilities with 60s refresh (`services/router.py`, `capabilities.py`); `llm_call_id` billing key + usage_records + transactional outbox + DLQ + disk journal replay (`db/outbox.py`, `services/billing_journal.py`); Valkey idempotency; per-tenant rate limiting; per-key ACLs (`services/acl.py`); BYOK `sealed:v1`/`env:` (`services/byok.py`); SSRF-hardened image fetcher.
- **Additive:** `POST /v1/rerank` + `POST /v1/classify` on the same auth/ACL/metering path.

**Partial/mocked:** `MOCK_PROVIDERS=true` (compose default); rerank/classify default to deterministic mock/`stub`; the plan→limits Auth-HTTP fallback is a **TODO stub** (`services/auth_client.py:160`, never invoked — the JWT `plan`-claim path works). Only `requests_per_min` is pre-flight; token limits are post-hoc debit.

**Missing vs spec:** smart routing (cheapest/fastest/capable); provider fallback chains; all other providers (Gemini/Groq/Azure/Mistral/Ollama/Bedrock); semantic response cache; budgets + hard-stop `402`; per-agent rate limits; predictive token limiting; provider health monitoring; PII log masking; `secretsmanager:` BYOK backend.

---

#### Guardrails — `Shared Core/guardrails` (Phase 04) — ~50%
**Footprint:** Python 3.12 / FastAPI (uv). **42 source files**, ~201 test functions / 29 files, **5 migrations**, Dockerfile (slim default + documented ml image), `eval/` harness.

**Implemented:** Full first-cycle + WP07 authoring + WP08 additive safety:
- `POST /v1/check/input` + `/v1/check/output` — always HTTP 200 with `allow|warn|redact|block`, precedence **BLOCK > REDACT > WARN > ALLOW**, short-circuit on first block (`services/pipeline.py`).
- 11 built-in rules (`services/rules/definitions.py`); deterministic `[REDACTED:cat:hmac]` redaction + key lifecycle + rotate (`core/redaction.py`, `api/redaction_keys.py`).
- Policy CRUD + assign + **simulate** (`api/policies.py`); custom tenant rules (regex/classifier-threshold) with ReDoS guard (`api/rules.py`); append-only policy versioning (`services/policy_engine.py`); `GET /v1/violations` redaction-safe paginated (`api/violations.py`).
- Post-response persist queue; transactional outbox + DLQ; policy cache + rate limiter; SLO instrumentation; RLS; dual-mode auth + revocation.
- **Additive (flag-gated, default-inert):** real-classifier cascade to llms-gateway; Presidio PII; prompt-injection defense with untrusted-span spotlighting; output groundedness; precision/recall eval harness.

**Partial/mocked:** default `CLASSIFIER_MODE=stub` (keyless); detoxify is opt-in prod only (falls back to stub if the checkpoint is absent); Presidio/groundedness/injection-defense inert by default; rate limiting default OFF.

**Missing vs spec:** remaining rule families (SSN/passport/name NER, hallucination, format validation); `semantic` custom-rule type (needs embeddings); async check mode (returns 422); topic blocklist; policy inheritance; hot reload; audit mode; trend dashboards; Llama Guard/Prompt Guard split pod; `window`/`post` streaming modes; xAgent embedded-library mode.

---

#### RAG — `Shared Core/rag` (Phase 05) — ~50%
**Footprint:** Python 3.12 / FastAPI (uv). **41 source files**, ~88 test functions / 15 files, **3 migrations**, Dockerfile, Kafka ingestion `worker/`, `eval/`. *(Spec note: RAG is explicitly **not** first-cycle-critical — it's WP09 pre-work for Phases 8–9.)*

**Implemented:** All first-cycle components:
- KB CRUD with immutable creation-time alias/dim resolution (`api/kbs.py`); inline ingest ≤100 KiB + presigned-upload + finalize with idempotency (`api/ingest.py`); Kafka ingestion worker + poison-pill/DLQ + s3-deletions sweeper (`worker/`); fixed+sentence chunking; embeddings via gateway + mock.
- pgvector HNSW two-pass CTE query, `top_k` cap 100 (`services/store/pgvector.py`, `IVectorStore` interface); per-principal KB ACLs; quota + usage metering; transactional outbox; RLS; platform-skills bootstrap loop.
- **Additive research upgrades SHIPPED (flag-gated, dense-only default):** hybrid dense+lexical via RRF (`search_mode=hybrid`); sparse/lexical (`tsvector`/GIN); cross-encoder rerank via llms-gateway (default off); contextual-retrieval ingest (default off).

**Partial/mocked:** `MOCK_EMBEDDINGS=true` default; `ObjectStore` uses hardcoded `minioadmin` creds + best-effort unsigned PUT (first-cycle local only); empty `S3_ENDPOINT` degrades put/head to no-op.

**Missing vs spec:** **PDF/parsing not implemented** (worker decodes UTF-8 text/markdown only); query expansion (HyDE/multi-query); remaining source types (URL/JSON/CSV/S3/db/webhook); semantic/recursive/code chunking; document versioning + re-ingestion; multimodal OCR + image embeddings; Pinecone/Qdrant adapters (any non-1536 dim raises `ValueError`); `rag.tenant_pricing`.

---

#### Memory — `Shared Core/memory` (Phase 06) — ~35% ⚠️ spec divergence
**Footprint:** Python 3.12 / FastAPI (uv). **34 source files**, ~96 test functions / 20 files, **3 migrations** (⚠️ **hand-rolled, NO Atlas** — no `atlas.hcl`/`schema.sql`, unlike the other Python services), Dockerfile, `eval/`. *(Spec note: also **not** very-first-cycle-critical — WP10 pre-work.)*

**Implemented (functionally):** `POST /v1/memories`, `POST /v1/memories/search`, by-id GET/PUT/DELETE, `POST /v1/sessions`, `POST /v1/gdpr/wipe`; idempotency before embed; principal-scoped visibility mirrored in SQL; dedup bump-only (≥0.95 cosine); quotas + hard ceilings; pgvector HNSW two-pass search; sessions with 409-on-collision; TTL sweep; RLS; transactional outbox; **404-not-403 anti-existence-leak**. Enterprise features (scoring, contradiction detection, consolidation) ship as **default-off placeholder skeletons**.

**⚠️ The one genuine spec-vs-code mismatch to flag** — the code **intentionally simplifies** the phase-06 contract:
- **Scope model:** code uses a 2-value `principal_only|tenant_shared` + tenant `user_scope_visibility`; spec mandates a **5-value** enum (`tenant/principal/agent/user/session`) with a `scope_id` UUID column + `importance` field.
- **Routes:** code has `/v1/memories/search`, `/v1/sessions`, `POST /v1/gdpr/wipe`; spec requires `/v1/memories/retrieve`, `/v1/memories/sessions`, query-param `DELETE /v1/memories?scope=&scope_id=`.
- **Storage:** code uses a plain repository, **not** the spec's `IVectorStore` + `memory.tenant_backends` seeded per tenant.

This is documented as intentional in `Shared Core/memory/CLAUDE.md`, but it means Memory's public contract does not match its spec. **Decision needed:** amend the spec to match, or migrate the API. (See §7 Phase A.)

**Missing vs spec:** auto-extraction (`/v1/memories/extract`), summarisation, working memory, importance scoring/decay, consolidation job, `user_scope_acl`, async `last_accessed_at` batching, re-embed job.

---

### 5.2 Agent Runtime

#### xAgent / ax-1 — `xAgent/ax-1` (Phase 09A) — 9A ~100%, full P09 ~60%
**Footprint:** Python 3.12 / FastAPI · psycopg3 (async, RLS) · aiokafka · Valkey. **54 source files**, 34 test files, **7 migrations** + `schema.sql`, multi-stage Dockerfile.

**Implemented (first-cycle 9A — all present):**
- **True stage-registry pipeline** (`core/pipeline.py`): `Stage` ABC, `PipelineContext`, ordered `STAGE_REGISTRY`, finally-style EVENT stage, between-stage + in-flight-LLM cooperative cancel.
- **Active stages:** `LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → EVENT`, writing exactly 3 audit rows.
- **Endpoints:** `POST /v1/tasks`, `GET /v1/tasks/{id}` (honest mid-run projection), `GET /v1/tasks` (cursor list), `DELETE /v1/tasks/{id}` (cooperative cancel), `GET/PUT/POST /v1/agents/{id}/runtime`, `GET /v1/capabilities`, health.
- **Reliability (WP08):** Contract-9 idempotency (Valkey SET NX, fail-closed), per-task timeout + background sweeper (atomic timeout+outbox), live revocation mirror, Contract-12 service-token minting + JWT forwarding, Valkey-cached Auth `/v1/authorize`, transactional outbox → `cypherx.agent.task.completed|failed`, RLS, W3C trace propagation, agent-config read-through cache, caller-vs-target guard (`body.agent_id == jwt.agent_id`).

**Implemented *beyond* first cycle (enterprise items done):** **SSE streaming** `GET /v1/tasks/{id}/stream` + **async mode** (`?mode=async`) — both tested (`test_wp12_sse.py`, `test_wp12_async_mode.py`); cursor task listing.

**Partial/gated:** **WP12 enhancement stages are fully coded AND bound, but disabled by default.** `core/stages/__init__.py` binds all 10 slots including `MEMORY_RETRIEVE`, `RAG_QUERY`, `SKILL_LOAD`, `TOOL_LOOP`, `MEMORY_WRITE`; the registry marks those five `enabled=False` and config defaults `stage_enable_*=False`. `tool_loop.py` (~570 lines) and `skill_load.py` are **real implementations**; supporting clients exist (`mcp_client.py`, `rag_client.py`, `memory_client.py`, `skill_registry_client.py`). ⚠️ **Compose does not inject `VALKEY_URL` into xagent**, so idempotency/cancel/cache degrade to fail-open paths in the default stack.

**Missing vs spec:** all of **9B (A2A)** and **9C (orchestration)** — that's ax-2/Phase-10 scope; `models/a2a.py` is only a Contract-3 *response builder*, not routing. Also: `cypherx.auth.agent.registered` consumer; guardrails circuit-breaker; per-step timing/cost dashboards.

#### xAgent / ax-2 — `xAgent/ax-2` (Phase 10) — 0% 🔴
Contains **only `CLAUDE.md`** (design placeholder) and `.git`. No `src/`, tests, migrations, or Dockerfile; not in compose. **Nothing** of `phase-10-a2a-orchestration.md` exists: chain-aware A2A JWT verification, consistent-hash router, `/v1/a2a/tasks`, workflow DAG engine (cycle-check, sequential/parallel/conditional/loop/HIL), `/v1/workflows*`, SSRF-validated callbacks. **By design** — Phase 10 is gated on 9A passing Contract-15 twice + 7 clean staging days.

---

### 5.3 Tools & Skills

#### Tool Registry — `Tools/tool-registry` (Phase 07 C1) — ~65%
**Footprint:** Python 3.12 / FastAPI. **23 source files**, ~12 test modules, **4 migrations**, Dockerfile. A real service (the in-repo `REPO_ANALYSIS_2026-06-11.md` describing a stub is **stale**).

**Implemented:** `GET /v1/tools` + `GET /v1/tools/{name}` (tenant-priority shadowing + `?version=` pinning), `POST /v1/tools` + `/versions` (Contract-4 validation, retention max 3), per-agent access control (`none|ask|automated`), restricted-tools registry, dual-mode JWT + revocation, **marketplace-hole split RLS with `WITH CHECK`**, 30s ETag-aware health poll with degrade/offline state machine + eager register poll. (The startup platform seed of `tool-web-search` was removed — public tools now register via the API; see migration `20260712_0008`.)

**Missing vs spec:** capability-resolver layer (`registry.capabilities` + `tenant_capability_bindings`); per-tenant quotas (`private_tools_max`/`invocations_per_min`); external-publisher submission (Trivy/Snyk scan, `pending_review→active`); S3 large-output offload; cosign image signing.

#### web_search flow-tool — `Tools/tool-flow-bridge` (Phase 07 C3 → Phase 5) — replaces the retired `tool-web-search`
The bespoke `tool-web-search` FastAPI service has been **decommissioned and removed** from the monorepo (service directory, compose service, registry platform seed, ECR/Doppler entries). Its `web_search` capability is now a **Public flow-tool**: a Node-RED flow (`Tools/tool-flow-bridge/src/tool_flow_bridge/assets/web_search_flow.json`) hosted on the singleton platform Node-RED runtime and exposed through the Flow-Tool-Bridge as the public MCP server **`mcp-web-search`** (tool `web_search`). Same contract (`{query, count?/max_results}` → `{results:[{title,url,snippet,rank}]}`), same providers (mock/serpapi/brave). Cutover + bootstrap: `Tools/tool-flow-bridge/docs/web-search-public-tool.md`.

#### Skill Registry — `Skills/skill-registry` (Phase 08) — ~30% 🟡 re-scoped
**Footprint:** Python 3.12 / FastAPI. **23 source files**, ~12 test modules, **4 migrations**, Dockerfile. Built as a near-mechanical **mirror of tool-registry** over a `skills` schema.

**Implemented:** `GET /v1/skills` + `/{name}` (shadowing + version pin), `POST /v1/skills` + `/versions`, per-agent access, restricted-skills, split RLS, dual auth + revocation, health. A **real working service** — but delivering only the *discovery + access-gating* slice.

**⚠️ Key finding — most of Phase 8 is absent:** no RAG/ingest, no template engine, no skill-step execution, no `action_type`/`action_ref`, no `tool-skill-retriever` MCP server, no CI indexing pipeline. Phase-08 specifies skills as YAML DAGs indexed into a `platform-skills` RAG KB, retrieved via a retriever MCP server, rendered by a template engine, executed step-by-step as an xAgent sub-pipeline. **None of that exists.** *(By plan, Phase 8 is excluded from first cycle and xAgent's `SKILL_LOAD` stays disabled — so there is no first-cycle bar to miss; this is an early, narrowed enhancement build.)*

---

### 5.4 Products (consuming apps — not numbered platform phases)

#### cypherx-a1 (+ mcp-eng-memory) — `CoreProjects/cypherx-a1` — MVP complete, ~45% of own roadmap
A first-class **consuming application** (peer of ax-1, *not* a SharedCore service), owning schema `cypherx_a1`. Ingests engineering history into a tenant-scoped **graph + RAG** corpus and serves a cited hybrid-retrieval copilot + a separate MCP facade.

**Footprint:** Python 3.12 / FastAPI. **~88 files** (55 in `src/cypherx_a1`, 17 in `mcp-eng-memory`), 11 test files, **5 migrations**, `openapi.yaml`, bundled UI console, `eval/`, 18 design docs, 2 Dockerfiles.

**Implemented (MVP):** `POST /v1/copilot/ask`; graph endpoints (`/v1/graph/{who-owns|what-breaks|experts|why-built|neighbors|activity}`); `POST /v1/connectors/{kind}/sync`; `POST /v1/extract` (LLM edge mining, bitemporal supersede); `POST /webhooks/{kind}`. Full pipeline: GitHub connector + fixtures; LAND→NORMALIZE→RAG-INGEST→LINK ingestion; knowledge-graph extraction/resolution; **RRF hybrid retrieval** (graph + dense + tsvector); cited copilot flow; RLS; outbox. **`mcp-eng-memory`** — standalone Contract-4 MCP server with 8 tools (`who_owns`, `what_breaks_if_changed`, `experts_on`, `why_built`, `graph_neighbors`, `what_changed`, + LLM-backed `incident_root_cause`, `how_does_x_work`).

**Partial/missing:** async Kafka worker (`worker/runner.py`) is a scale-out seam — MVP drives ingestion synchronously; default `CONNECTOR_MODE=mock`; **Jira/Slack connectors not built**; enhancement phases A–D + KG-accuracy are design-stage.

---

### 5.5 Frontend — `frontend/` (Phase 12) — ~85% first-cycle / ~40–45% full

**`frontend/app`** — Next.js 15 / React 19 / TS 5.7 SPA ("CypherX Console"). ~60 source files (~33 routes, ~19 components incl. a `ui/` primitive library), 4 test files. Talks **only** to the BFF via one chokepoint (`lib/bff-client.ts`). Screens present: login, register + register/verify, dashboard, agents + AgentBuilder (two-step publish), keys, tasks + run + detail, guardrails + PolicyEditor, usage, llms + aliases, audit, health, **plus** orchestrator, hil (approvals), tenant, rag.

**`frontend/bff`** — Node ≥22 / Fastify 4, the browser↔platform trust boundary. 21 source files, **10 test files (~69 cases)**. Holds the agent JWT server-side in an AES-256-GCM Valkey session (SPA never sees a token); `/bff/login|logout|me`; opaque `/bff/api/<svc>/*` proxy with header strip/inject; **SSE relay** for task streams; double-submit + session-bound CSRF (timing-safe); full CSP/HSTS/COOP/CORP; health + metrics.

**`frontend/demo`** — zero-dependency stdlib-Python (`http.server`) harness (`server.py` 363 lines) that drives auth→xagent→llms→guardrails directly. A prototype smoke tool, **not** the product; binds loopback.

**⚠️ Login-mechanism drift (needs reconciliation).** Three inconsistent accounts exist across the repo:
1. `frontend/CLAUDE.md`: a tenant/agent/API-key "platform-credential" login with the Agent-ID field missing → *broken*.
2. `E2E_TEST_REPORT_2026-06-14.md`: platform-credential login **works** (Agent-ID field renders, 59/59 pass).
3. `END_TO_END_WALKTHROUGH.md` (ground-truth from source): platform-credential login **replaced entirely** by email/password + Google OAuth + self-serve register.

The app has `login`/`register`/`register/verify` pages and Auth ships `UserAuthController.kt` with Google OAuth, so **email/password + OAuth is the shipped direction** — but the BFF audit still describes a platform-credential provider. **Action:** run the stack, confirm the live login path, and fix the stale docs (see §8).

**Missing vs spec:** Memory/Skills/Tools dashboards (gated on Phases 6/7/8); workflow canvas (Phase 10); multiplexed tenant-wide SSE feed; px0 SSO provider (`BFF_AUTH_PROVIDER` switch); px0 billing iframe; cloud deploy-target frontend module.

---

### 5.6 Contracts — `contracts/` (Phase 00) — ~95–100% ✅ (strongest area)
**Footprint:** Node/ESM with an **Ajv (draft 2020-12) validation harness** (`scripts/validate.mjs`), Redocly OpenAPI lint, GitHub Actions + GitLab CI. ~70 artifact files across all 21 named subfolders + examples + a 21 KB Postman collection + a 28 KB guardrails golden suite.

**All 21 contracts have concrete artifacts** (none are placeholders): JWT claims, error format, A2A message (request/response/delegation + task-types), MCP manifest, Kafka envelope (+ 11 event schemas), log format, health/metrics, trace headers, versioning/pagination, OpenAPI base, skill definition, service token, tenant model, migration standard, first-cycle smoke test (+ Postman), step-up approval token, behavioral policy, API-key/ACL, usage metering + quotas, external onboarding, outbound webhook. Extras beyond the 21: `classify`/`rerank` schemas, golden-suite JSONL, jailbreak-leak patterns, billing rule-cost. The phase-00 repo-structure map matches disk 1:1. **Nothing significant missing.**

---

### 5.7 Infrastructure — `infra/`, `charts/`, `gitops/`

**`infra/compose` (Phase 1 runtime) — ~95% ✅.** The definitive local platform: 27 services (6 backing infra, 11 app, 5 edge/frontend/jobs, 5 observability) against external Neon. Includes `migrate.sh` (ordered per-service migrations + runtime `*_user` roles), `topics-init`, `minio-init`, edge Caddyfile, and a full observability profile. A `docker-compose.realembed.yml` overlay flips llms/rag/memory to real OpenAI embeddings.

**`infra/` cloud IaC (Phase 1 deploy) — ~85–90% authored, 0% applied 🟡.** ~200 files. **13 Terraform modules** (vpc, eks-cluster, postgresql, valkey, kafka, iam, ecr, dns, s3, tfstate-backend, postgres-bootstrap, kafka-topics, doppler-bootstrap); **~19 k8s-addons** (istio, kong, argocd, cert-manager, aws-lbc, karpenter, external-dns, kube-prometheus-stack, loki, tempo, promtail, doppler-operator, namespaces, network-policies); Terragrunt `environments/` for dev/staging/prod; 5 GitHub Actions workflows. **No AWS resources exist** — IaC never applied; the Component-21 smoke test has never run against a real env, so the Phase-1 "2 consecutive passing runs" exit gate is **unmet**. `dev/local/Tiltfile` service blocks are guarded (deps-only).

**`infra/` hardening (Phase 13) — ~5–10% 🔴.** All 9 domains (security audit/WAF, load testing, rate-limit/quotas, public API docs site, sandbox/marketplace, operational readiness/SLO/status page, SPIFFE migration, DR/BCP, SOC2/GDPR/ISO) are 📋. Only fragments exist: per-PR Trivy scans in CI, default-deny NetworkPolicies in charts/addons.

**`charts/` (Phase 1) — ~90–95%.** `cypherx-service` base chart: 12 templates (deployment with non-root/RO-rootfs/drop-ALL-caps, migration-job Atlas hook, dopplersecret, service, SA, hpa, pdb, servicemonitor, networkpolicy default-deny, optional istio vs/dr) + strict `values.schema.json` (`additionalProperties:false`). Encodes Contracts 6/7/8/13/14. `example-service` is a working values-only consumer for CI. Missing: `Chart.lock`/vendored subchart, published OCI registry, richer tests. **Not in compose** (K8s-only).

**`gitops/` (Phase 1) — scaffold complete, ~0% app population 🟡.** 3 root Application manifests; dev/staging carry `syncPolicy.automated`, **prod deliberately omits it** (manual-sync safety gate). `envs/{dev,staging,prod}/` and `base/` hold only `.gitkeep`. **Zero service child-apps** — those arrive per owning deploy phase.

---

### 5.8 Platform-Management Control Plane — `platform/` (Phase 11) — 0% 🔴
Intended `platform-service` control plane (aggregate observability, versioned config/deploy management, cost roll-up, px0 billing push, quota governance, ArgoCD-webhook rollback). **Footprint: exactly two files** — `platform/README.md` (unmodified **GitLab boilerplate**) + `platform/CLAUDE.md` (design notes). No `src/`, build files, Dockerfile, migrations, tests; not in compose. Intended stack per spec is **Kotlin + Spring Boot**. Entirely unbuilt — by design (Phase 11 is 📋, to begin after core services are operational).

---

## 6. First-Cycle Definition of Done — Contract-15

`contracts/smoke-tests/first-cycle.md` is the platform's "unambiguous definition of done": the 15-case scenario must pass **twice** against a freshly cold-deployed environment. Cases **1–10** gate the core spine (Phases 0–4 + 9A); cases **11–15** gate the enterprise wave (onboarding, idempotency, rate limiting, OIDC discovery).

**Status: the automated E2E suite passes 59/59** (`E2E_TEST_REPORT_2026-06-14.md`), after two real bugs were fixed:
- **BUG-1 (CORS blocker):** `NEXT_PUBLIC_BFF_URL` baked the raw BFF port into the SPA, breaking same-origin through Caddy → fixed in `.env.example` + `docker-compose.yml` (empty default → relative `/bff`).
- **BUG-3 (scope escalation):** Auth issued API keys with scopes broader than `agent.allowed_scopes` → fixed in `ApiKeyService.kt` (`validateScopesAgainstAgent()` rejects with 403).

| # | Scenario | Proves | Status |
|---|---|---|---|
| 1 | "What is 2+2?" | Whole spine: 200, answer has "4", tokens/cost > 0 | ✅ |
| 2 | Prompt-injection | `422 GUARDRAIL_VIOLATION` before the LLM runs | ✅ |
| 3 | Message with an email | 200, email redacted before the model | ✅ |
| 4 | Tenant B reads Tenant A's task | **404, not 403** | ✅ |
| 5 | No `Authorization` header | 401 at the edge | ✅ |
| 6 | 5 tasks → read Kafka fresh | Exactly 5 usage events, matching trace_ids | ✅ |
| 7 | Completed task | steps `[input, llm_call, output]` in order | ✅ |
| 8 | Search traces by trace_id | One trace spans xAgent→Guardrails→LLMs→provider | ✅ |
| 9 | Recent logs for `service=xagent` | 100% valid structured JSON | ✅ |
| 10 | `/livez`+`/readyz` on 4 spine svcs | All 200, correct shape | ✅ |
| 11 | Onboarding → sandbox key → chat | All 2xx; `source='self-serve-signup'` | ✅ |
| 12 | Replay same Idempotency-Key + body | Replays cached response, no 2nd LLM call | ✅ |
| 13 | Same key, different body | `409 IDEMPOTENCY_KEY_CONFLICT` | ✅ |
| 14 | Exceed free-tier rate limit | 429 with `Retry-After`/`X-RateLimit-*` | ✅ |
| 15 | OIDC discovery document | Valid, spec-shaped JSON | ✅ |

**Remaining ceremony:** the formal DoD is "2× cold on a fresh deploy." The E2E fixes were validated by code inspection + one live run; the definitive gate is a clean **2× cold-deploy** run (part of WP14). Also note the E2E run exercised the compose stack — the *cloud* environment gate (§5.7) is entirely separate and unmet.

---

## 7. What's Next — Phased Roadmap

The execution plan's WP01–WP14 ("first-cycle 100%") is **largely already delivered** — Shared Core, xAgent reliability (WP08), RAG (WP09), Memory (WP10), Tools (WP11), the coded enhancement stages (WP12), the frontend (WP13), and compose+observability (WP14) all exist. So "next phases" is best framed as four forward tracks:

### Phase A — Close out & harden first cycle *(weeks)*
Small, high-leverage items to make "first cycle" airtight:
- **Reconcile Memory to its contract** — either amend `phase-06` to bless the 2-value scope model, or migrate the API to the spec's 5-value `scope`+`scope_id`+`importance` and `/retrieve`//`/extract` routes + `IVectorStore`. *(Blocking clarity for anything that consumes Memory.)*
- **Inject `VALKEY_URL` into xagent in compose** — restore fail-closed idempotency/cancel/authorize-cache (today they silently fail-open).
- **Settle the frontend login drift** — run the stack, confirm the live login path, fix `frontend/CLAUDE.md` + BFF provider docs.
- **Run Contract-15 2× cold** as the formal DoD gate; re-run the Playwright E2E after a fresh `frontend-app` rebuild.
- **Verify WP12 enhancement stages end-to-end** in a real-embeddings profile (flip the `stage_enable_*` flags, exercise RAG_QUERY/MEMORY/TOOL_LOOP against the live stack).
- **tool-registry:** add capability-resolver + per-tenant quotas.
- **Stale-doc cleanup** (see §8).

### Phase B — Big net-new builds *(the real "next phase")*
The three genuine greenfield capabilities:
- **`xAgent/ax-2` — A2A router + Orchestrator (Phase 10).** The single largest capability gap. Chain-aware A2A JWT verification, consistent-hash routing to ax-1 pods, `/v1/a2a/tasks` (sync/async/stream), a workflow DAG engine (cycle-check, sequential/parallel/conditional/loop/HIL), `/v1/workflows*`, SSRF-validated callbacks. Unlocks true multi-agent delegation. *(Gated by plan on ax-1 passing Contract-15 2× + 7 clean staging days.)*
- **`platform/` — control plane (Phase 11, Kotlin/Spring).** Service registry, append-only versioned config + outbox, `tenant_costs`/running-totals, idempotent px0 billing push, ArgoCD-webhook rollback, quota-breach events, the `px0-bridge` sibling.
- **Full Skills execution (Phase 8).** Turn the catalogue-only registry into the specified engine: RAG-indexed `platform-skills` KB, a `tool-skill-retriever` MCP server, a template engine, and skill-step execution as an xAgent sub-pipeline (then enable `SKILL_LOAD`).

### Phase C — Enterprise deepening per service *(the "Explicitly excluded" list)*
Each already-built service has a substantial enterprise checklist remaining:
- **Auth:** SPIFFE/workload identity, OPA/ABAC policy engine + `/v1/policies` API, behavioral-policy enforcement, token binding (`cnf`/DPoP), single-use `jti`, federated IdPs, full approval-flow enforcement, audit partitioning, tenant hard-delete.
- **LLMs:** smart routing + provider failover, more providers (Gemini/Groq/Azure/Mistral/Ollama/Bedrock), semantic response cache, budgets + hard-stop 402, per-agent rate limits, PII log masking, provider health monitoring, `secretsmanager:` BYOK.
- **Guardrails:** NER PII rules (SSN/passport/name), semantic topic blocklist, output-hallucination LLM judge, async/window/post streaming modes, policy inheritance, hot reload, Llama Guard split pod, trend dashboards.
- **RAG:** query expansion (HyDE), all source types (URL/JSON/CSV/S3/webhook/**PDF**), semantic/recursive/code chunking, document versioning + re-ingestion, multimodal OCR/image embeddings, Pinecone/Qdrant adapters.
- **Memory:** the 5-value scope model, auto-extraction (`/extract`), summarisation, working memory, importance decay, consolidation job, `user_scope_acl`, re-embed job.
- **xAgent:** enable the enhancement stages in production, add the guardrails fail-open circuit breaker, the `agent.registered` consumer.
- **Tools:** external-publisher submission (Trivy/Snyk/sandbox), marketplace publish/install, and the ~11 other tool servers (code-exec/gVisor, http-client, file-ops, email, calendar, browser, image-gen, pdf-gen, data-analysis, notify), Anthropic-MCP bridge.

### Phase D — Production readiness *(parallelizable with C)*
- **Phase-13 hardening:** security audit/WAF, load testing vs the Guardrails 30/50ms SLOs, DR/BCP, SOC2/GDPR/ISO compliance, status page + SLOs, public API docs site, sandbox/marketplace.
- **Cloud deployment:** apply the Terraform (stand up EKS/RDS/MSK/ElastiCache), run the Phase-1 infra smoke gate **2×**, populate `gitops/` service child-apps, publish the Helm chart to an OCI registry, wire Kong/Istio, Argo Rollouts canary, HPA/KEDA, egress NetworkPolicies, Doppler operator.
- **SDKs (Phase 14):** per-service Python + npm packages over a shared `cypherx-core` runtime + a meta-package — deferred until the APIs stabilize post-P13.

### Product track — cypherx-a1 *(independent of platform phases)*
Jira/Slack connectors, the live Kafka async worker for at-scale ingestion, and enhancement phases A–D + KG-accuracy.

---

## 8. Cross-Cutting Risks & Known Gaps

**Engineering risks (from the execution plan, still live):**
- **Neon latency vs Guardrails SLOs.** Remote pooled Postgres + new per-request Valkey/DB work threatens the 30/50ms input SLOs — must be re-validated under load (Phase D load test).
- **Valkey is now load-bearing for fail-closed semantics** (xAgent idempotency `503`, guardrails rate limiting). A Valkey outage degrades writes platform-wide; persistence/auth hardening + capacity sizing land late. **Compounded by the compose gap** where `VALKEY_URL` isn't injected into xagent (Phase A fix).
- **detoxify/torch packaging** (~2GB ml image, baked checkpoint, eager-load startup) — risk of CI/registry pain; the slim/ml split must be enforced or prod silently runs the keyword stub.
- **Streaming-correctness surface** (tool-call aggregation + SSE-through-Valkey) is the most regression-prone area — golden-test before any refactor.
- The **`llm_call_id` billing-key migration** on the live revenue-bearing spine appears landed (present in `Shared Core/llms`), but any further change there needs replay-journal compatibility proofs.

**Documentation drift to fix** (source is correct; docs are stale):
- `frontend/CLAUDE.md` — describes a platform-credential login the code appears to have replaced with email/password + Google OAuth (§5.5).
- `Shared Core/memory/CLAUDE.md` — documents the intentional-but-unblessed API divergence from `phase-06` (§5.1).
- `Tools/tool-registry/REPO_ANALYSIS_2026-06-11.md` — describes a stub; the service is real.
- `Skills/skill-registry/db/migrations/README.md` — lists 2 old migrations; 4 exist.
- `Shared Core/llms/README.md` + `db/migrations/README.md` — call BYOK/SSRF "deferred" though both are built.
- `CoreProjects/cypherx-a1/openapi.yaml` — omits `/v1/graph/activity`, which is live and used by the MCP manifest.

---

## 9. Appendices

### 9.1 Phase → service → code mapping

| Phase | Name | Code location | Status |
|---|---|---|---|
| 0 | Contracts & Standards | `contracts/` | ✅ ~100% |
| 1 | Infrastructure | `infra/compose` (run) · `infra/{modules,environments}` (cloud) · `charts/` · `gitops/` | ✅ compose · 🟡 cloud 0% applied |
| 2 | SharedCore / Auth | `Shared Core/auth` | ✅ first-cycle · ~60% |
| 3 | SharedCore / LLMs | `Shared Core/llms` | ✅ first-cycle · ~45% |
| 4 | SharedCore / Guardrails | `Shared Core/guardrails` | ✅ first-cycle · ~50% |
| 5 | SharedCore / RAG | `Shared Core/rag` | ✅ first-cycle · ~50% |
| 6 | SharedCore / Memory | `Shared Core/memory` | 🟡 functional, spec-divergent · ~35% |
| 7 | Tools (MCP) | `Tools/tool-registry` · `Tools/tool-flow-bridge` | ✅ registry ~65% · flow-tools + public `web_search` (replaced `tool-web-search`) |
| 8 | Skills System | `Skills/skill-registry` | 🟡 re-scoped ~30% |
| 9 | xAgent Core | `xAgent/ax-1` | ✅ 9A ~100% · full P09 ~60% |
| 10 | A2A & Orchestration | `xAgent/ax-2` | 🔴 0% (empty) |
| 11 | Platform Management | `platform/` | 🔴 0% (stub) |
| 12 | Frontend | `frontend/{app,bff,demo}` | 🟡 ~85% first-cycle · ~40–45% full |
| 13 | Hardening | `infra/` (fragments) | 🔴 ~5–10% |
| 14 | SDKs | *(none)* | 🔴 0% |
| — | **Product:** cypherx-a1 | `CoreProjects/cypherx-a1` | ✅ MVP · ~45% own roadmap |

### 9.2 Contracts (1–21) status
All present with concrete artifacts (§5.6). Doc-type by design (Markdown, not payload schemas): 7 (health), 8 (trace headers), 13 (tenant model), 20 (onboarding), 21 (webhooks). Everything else is JSON Schema / YAML / OpenAPI with an executing Ajv validator + CI on GitHub and GitLab.

### 9.3 Test-footprint summary

| Service | Tests | | Service | Tests |
|---|---|---|---|---|
| auth | 30 `@Test` / 10 suites | | ax-1 | 34 test files |
| llms | ~200 fns / 27 files | | tool-registry | ~12 modules |
| guardrails | ~201 fns / 29 files | | tool-flow-bridge | (flow-tools + web_search) |
| rag | ~88 fns / 15 files | | skill-registry | ~12 modules |
| memory | ~96 fns / 20 files | | cypherx-a1 | 11 files |
| frontend/bff | ~69 cases / 10 files | | frontend/app | 4 files |
| **E2E (Playwright)** | **59/59 passing** (5 spec files) | | contracts | Ajv fixture suite |

### 9.4 Primary sources
`archive/Manoj/phases/{README, phase-00..14, EXECUTION_PLAN_FIRST_CYCLE_100, E2E_TEST_REPORT_2026-06-14, AUDIT_REPORT}.md`, `archive/Manoj/phases/amendments/plan-fixes.json`, root `END_TO_END_WALKTHROUGH.md`, and direct reads of service source, routers, pipeline code, migrations, tests, Dockerfiles, and `infra/compose/docker-compose.yml`.

---

*Report compiled 2026-07-09 from three parallel source-code audits cross-checked against the phase specs and the ground-truth walkthrough. Completeness percentages are engineering estimates of full-enterprise-spec coverage; first-cycle status reflects presence and tested behavior of the minimum viable slice. Where planning docs and code disagreed, the code was followed.*