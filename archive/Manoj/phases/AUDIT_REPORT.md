# CypherX AI — Phases Audit Report
> Date: 2026-05-23 | Reviewer: Claude (Opus 4.7) | Status: Applied

This report covers an end-to-end review of the 15 phase documents in `archive/Manoj/phases/` plus the two master docs (`CYPHERX_AI_PLATFORM_PLAN.md`, `CYPHERX_AI_ENTERPRISE_FLOW.md`). The objective was to (a) verify HLD/LLD quality, (b) verify the First Cycle implementation scope is internally consistent, and (c) fill gaps that would block implementation.

---

## 1. Overall Assessment

**Quality: strong baseline.** The structure is consistent across all 15 phases — Overview → HLD with system context diagram → LLD with data models, APIs, components → K8s deployment spec → First Cycle / Full Enterprise checklists. The phase-marker convention (⚡ first cycle vs 📋 full enterprise, plus 🏗️ "service architecture planned separately") is good and used consistently. Build order (Contracts → Infra → Auth → LLMs → Guardrails → xAgent core) is sound; dependencies are explicit; cross-cutting invariants (mTLS everywhere, tenant_id everywhere, JWT on every endpoint, /health /ready /metrics on every service) are explicit and consistent.

**Where it needed work:** the first-cycle scope had contradictions that would have caused real implementation pain (xAgent depending on tools and memory that were 📋), several first-class concerns were unspecified (tenant model, service-to-service auth, schema migration tool, local dev story), and a handful of "shape this so future-you doesn't regret it" details were missing (vector-dimension shard strategy, LLM tool-use in the unified schema, output-guardrail rules, fail-open/closed decision).

All issues called out below have been **applied as edits** to the relevant phase docs.

---

## 2. Critical Fixes Applied

### 2.1 First-Cycle Scope Contradictions (Phase 9)

**Problem:** `phase-09-xagent.md` first-cycle checklist included "Tool-use loop (MCP client for tool invocation)" and the execution engine performed memory injection — but Phase 7 (Tools) and Phase 6 (Memory) are entirely 📋, so neither would exist when xAgent was being built. A team following the checklist would either be blocked or would build half a Tool Registry to unblock themselves.

**Fix:**
- Added explicit **Scope Boundary** section at the top of Phase 9 stating first-cycle is "LOAD → PRE-GUARDRAIL → LLM (single round-trip, no tools) → POST-GUARDRAIL → RETURN".
- Refactored the execution engine in Component 3 to be a **pipeline of named stages**, each ⚡ or 📋. First-cycle stages are wired; enhancement stages (`MEMORY_RETRIEVE`, `SKILL_LOAD`, `TOOL_LOOP`, `MEMORY_WRITE`) are defined in the pipeline but disabled by config until their dependencies exist. This means the engine is structured correctly from day one; later phases add stages, not rewrites.
- Rewrote the Phase 9 first-cycle checklist to remove the contradictions and added "Passes all 10 cases of Contract 15 (First-Cycle Smoke Test)" as the exit criterion.

### 2.2 Service-to-Service Auth Was 📋 But Needed in First Cycle (Phase 0, Phase 2)

**Problem:** Every first-cycle service (xAgent → Auth/Guardrails/LLMs, LLMs → Auth, Guardrails → Auth) makes inter-service calls. Each call must be authenticated as itself, not just by passing an agent JWT through. Contract 12 (service-to-service token format) was 📋 — defined but not first-cycle. Phase 2 (Auth) had no endpoint to issue service tokens. There was no way to actually authenticate inter-service traffic in first cycle.

**Fix:**
- **Phase 0 Contract 12** promoted to ⚡ first cycle, with concrete acquisition flow: SPIFFE/K8s ServiceAccount TokenReview (preferred) or bootstrap shared secret (acceptable for first cycle, mandatory replacement before Phase 13).
- **Phase 2** gained a new **Component 8b — Service Token Issuance** with `POST /v1/service-tokens`, `auth.service_acl` table seeded for first-cycle service edges, and the issuance flow documented.
- **Phase 2 Auth integration pattern** rewritten as a two-layer model: Layer A (local JWT verify, scope check — always) + Layer B (remote `/authorize` — only for stateful checks). This avoids a network round-trip on every hot-path call while keeping the security model strong.

### 2.3 Tenant Model Was Implicit (Phase 0)

**Problem:** `tenant_id` appeared in every JWT, every DB row, every Kafka event, every log line — but its definition was never stated. Was it the same as px0's `org_id`? When was a tenant created? What did "platform tenant" mean for system-owned resources like the Skills knowledge base? Without this contract, every service would pick its own answer.

**Fix:**
- **Phase 0 Contract 13 — Tenant Model & ID Resolution** added as ⚡ first cycle. Defines: `tenant_id == px0.org_id` everywhere except platform-owned resources (which use the well-known UUID `00000000-0000-0000-0000-000000000001`). Includes tenant lifecycle (Kafka events from px0), enforcement rules (RLS policy on every tenant-scoped table, `SET LOCAL app.tenant_id` per transaction), and explicit anti-patterns.

### 2.4 Schema Migration Tool Was Unspecified (Phase 0)

**Problem:** Every service has SQL schemas. No tool was chosen, no convention was set. Teams would each pick (or not pick) their own, leading to migration drift in CI/CD.

**Fix:**
- **Phase 0 Contract 14 — Schema Migration Standard** added as ⚡ first cycle. Tool: **Atlas** (atlasgo.io). Convention: `<service>/db/migrations/<timestamp>__name.sql` + declarative `schema.sql`. CI gates: lint blocks destructive changes without an explicit nolint, schema-diff blocks drift, integration tests against real PostgreSQL. Runtime: K8s `Job` as Helm pre-install hook with privileged DDL user; service runtime uses least-privilege user.

### 2.5 No First-Cycle Smoke Test (Phase 0)

**Problem:** "First cycle done" was subjective. No concrete test scenario, no exit criterion an engineer could verify.

**Fix:**
- **Phase 0 Contract 15 — First-Cycle Smoke Test** added. 10 concrete test cases covering happy path (LLM call), prompt injection block, PII redaction, cross-tenant isolation, missing auth, Kafka event integrity, audit log content, distributed trace visibility, log format compliance, and all-services health. Exit criterion: all 10 pass twice on a cold-deployed dev environment. Phase 9 first-cycle checklist now references this contract as its definition of done.

### 2.6 Unified LLM Schema Missing Tool-Use & Multi-Modal (Phase 3)

**Problem:** The unified `/chat/completions` schema had no `tools[]`, `tool_choice`, or `tool_calls` fields. Phase 9's execution engine assumed OpenAI-style `tool_calls` in responses. Anthropic uses `tool_use` content blocks. Without provider normalization, xAgent would not work portably. Also, no multi-modal `image_url` support — adding it later would break the schema.

**Fix:**
- **Phase 3 Component 1** rewritten with full OpenAI-style superset schema including `tools[]`, `tool_choice`, `tool_calls`, multi-modal `image_url` content blocks, `response_format`, and explicit cost-calculation formula.
- Mandatory normalization rules added: Anthropic adaptor MUST convert `tool_use` → `tool_calls` and `stop_reason: "tool_use"` → `finish_reason: "tool_calls"`. OpenAI adaptor MUST set `stream_options: { include_usage: true }` on streaming calls.
- Per-provider streaming token-usage rules added (Anthropic vs OpenAI vs others); tool-call streaming assembly rule (don't stream partial JSON to clients) added.
- First-cycle checklist updated to require tool-use schema support and multi-modal schema (even if the first agent doesn't use either, the schema must be open from day one).

### 2.7 Vector Dimension Hardcoded (Phase 5, 6)

**Problem:** Both RAG and Memory had `embedding vector(1536)` hardcoded — tightly coupled to OpenAI `text-embedding-3-small`. Switching embedding models or supporting larger-dim models (`-large` = 3072) would require schema rewrite.

**Fix:**
- **Phase 5** and **Phase 6** now use a metadata table + per-dimension vector tables pattern (`chunks` + `chunk_vectors_1536`, `chunk_vectors_3072`, …). The KB / tenant pins its embedding model at creation; switching = re-embed background job. Also switched IVFFlat → **HNSW** index (better recall, no training step).

### 2.8 Guardrails — Output Rules, Fail-Mode, Streaming (Phase 4)

**Problem:**
- `/v1/check/output` was ⚡ first cycle but no output rules were listed (all 6 first-cycle rules were input-only).
- No fail-open vs fail-closed decision — the most important security choice in the whole guardrails service was undefined.
- Streaming output guardrails were not addressed — by the time a violation is detected on a streamed response, unsafe content may already be on the wire.
- No deterministic PII redaction format (so downstream code can't match redacted spans).

**Fix:**
- Added **5 first-cycle OUTPUT rules** (output-pii-email, output-pii-credit-card, output-toxicity, output-jailbreak-leak, output-max-length).
- Added baseline **ML classifier recommendations** (Llama Guard 2 / Prompt Guard / detoxify) served behind a single internal `/classify` endpoint.
- Added **Component 5b — Service Availability Decision (Fail-Open vs Fail-Closed)** — explicit `fail_mode: closed | open | circuit_break` per rule, with platform-default fail-closed for `block` rules.
- Added **Component 5c — Streaming Output Guardrails** — three strategies (`buffer`, `window`, `post`), each rule declares which it uses.
- Added deterministic redaction format `[REDACTED:<category>:<HMAC-token>]` with per-tenant HMAC keys.

### 2.9 RAG File Upload via Service Pod (Phase 5)

**Problem:** Ingestion endpoint implied multipart-to-service file uploads. 50MB PDFs would consume API gateway and service pod memory unnecessarily.

**Fix:**
- Switched to **pre-signed URL ingest** flow: `POST /upload-url` → client PUTs to S3 → `POST /finalize` enqueues Kafka job. Small inline endpoint kept for markdown/text < 100KB.
- Ingestion queue explicitly **Kafka, not in-memory** — workers commit offsets only after successful indexing, so pod crashes don't lose ingestion jobs.

### 2.10 Missing K8s Operational Add-ons (Phase 1)

**Problem:** No metrics-server (HPA prerequisite — without it, all HPAs sit at "unknown"). No cluster autoscaler / Karpenter (HPA scales pods, no new nodes appear). No external-dns (every new ingress hostname needs a manual Route53 entry). No reloader (rotated Doppler secrets need manual pod restart). All standard infra but easy to miss.

**Fix:**
- **Phase 1 Component 17b — K8s Operational Add-ons** added: metrics-server, Karpenter, external-dns, reloader — all ⚡ first cycle. cluster-autoscaler listed as fallback.

### 2.11 No Local Development Story (Phase 1)

**Problem:** Engineers would need to develop against a shared dev cluster — guaranteed to slow everything down and cause friction.

**Fix:**
- **Phase 1 Component 17c — Local Development Story** added: Tilt + kind + Docker Compose for dependencies. Redpanda substitutes for Kafka, MinIO substitutes for S3, Istio/Kong are skipped locally (direct service-to-service DNS). `tilt up` brings up SharedCore on a laptop in <5 minutes.

### 2.12 JWKS Rotation Procedure Undefined (Phase 2)

**Problem:** JWKS endpoint exposed but no procedure for *when* and *how* to rotate signing keys.

**Fix:**
- Added concrete JWKS rotation procedure: `auth.signing_keys` table with `status: active | retiring | retired`, 90-day default rotation, retiring window = max(JWT TTL) + 1h, KMS-encrypted private keys.

### 2.13 MCP Protocol Ambiguity (Phase 7)

**Problem:** "MCP" referenced throughout but is a custom HTTP+JSON REST protocol, not Anthropic's JSON-RPC MCP spec. Engineers might assume compatibility with Claude Desktop / IDE plugins. Future bridge work needs to know this is a deliberate choice.

**Fix:**
- Added Protocol Naming Clarification at the top of Phase 7 distinguishing CypherX MCP (CMCP — HTTP+JSON, multi-tenant, behind Istio) from Anthropic MCP (JSON-RPC over stdio/SSE). Future `tool-mcp-bridge` will translate outward. Endpoint paths versioned: `/mcp/v1/invoke`, `/mcp/v1/manifest`.

### 2.14 Tool Versioning Strategy (Phase 7)

**Problem:** Spec said versioning was important but didn't say how it worked operationally.

**Fix:**
- Added concrete tool-versioning model: one row per (tool_name, version) in registry; per-version K8s Deployments + Services; agents pin `"tool-web-search@1.2.0"` or `"@latest"`; deprecation emits `Deprecation` / `Sunset` headers.

### 2.15 Skills — Template Engine & Platform Tenant (Phase 8)

**Problem:**
- Skill step inputs used `{{...}}` but the template language was not defined.
- skills-kb in RAG needed a `tenant_id`, but no "platform tenant" concept existed — RAG would either need a special case or skills would be confined to one real tenant.

**Fix:**
- **Phase 8 Component 4** now references the platform-tenant UUID from Contract 13 with the single documented cross-tenant-read exception (platform-tenant KBs, READ only, audit-logged).
- **Phase 8 Component 4b — Template Engine** added: Jinja2/Pongo2-compatible templating with explicit allowed feature set (variable expansion, limited filters, conditionals only — no loops, no I/O), 100ms execution limit, 64KB output cap.

### 2.16 A2A Pod Routing & Cancellation (Phase 10)

**Problem:**
- A2A messages addressed agents by `agent_id` but agents are not K8s Services — they are data inside a multi-tenant agent-runtime Deployment. How does the request reach the right pod?
- Workflow cancel just refused new subtasks; running LLM calls / tool calls continued for minutes, racking up cost.

**Fix:**
- **Phase 10 Component 0 — A2A Routing Model** added: a2a-router does name → pod routing with consistent hash on agent_id (cache locality); agents are data, all pods serve any agent.
- **Phase 10 Component 5b — Cancellation & Timeout Propagation** added: cancel signal published to Kafka topic `cypherx.agent.task.cancel.requested`; every agent-runtime pod consumes it and cancels matching in-flight LLM / MCP calls; recursive cancel through a2a-router for delegated subtasks.

---

## 3. Issues Identified But NOT Yet Fixed

These are real but lower priority — they don't block first cycle.

| Issue | Phase | Risk if left | Recommended action |
|-------|-------|-------------|-------------------|
| Streaming `/v1/tasks/{id}/stream` endpoint in xAgent uses Valkey pub/sub — does not scale cross-cluster | 9 | High once multi-region | Move to Kafka-backed event log per task, or pinned routing |
| Compute / storage cost attribution model | 11 | Inaccurate billing | Decide tagging vs proportional model before first paying customer |
| px0 billing event retry / DLQ if px0 is down | 11 | Lost billing events | Add Kafka retry topic with exponential backoff |
| Frontend technology choice (Next.js / Remix / etc.) | 12 | Slowdown when Phase 12 starts | Decide before Phase 12 design begins |
| SOC2 / GDPR / HIPAA compliance scope | 13 | Enterprise sales blocker | Decide during Phase 13 planning |
| Disaster recovery plan (RPO/RTO, region failover) | 13 | Outage extends beyond RDS backup window | Add a DR runbook |
| Chaos / failure-injection testing | 13 | Untested failure modes | Add to Phase 13 hardening |
| Browser-safe SDK authentication (API key in browser = leak) | 14 | Security incident if mishandled | Document "browser uses user JWT only, never raw API key" |

---

## 4. Cross-Cutting Concerns Worth Calling Out

These are properties that span all services. They were already largely covered by Phase 0 and the Enterprise Flow doc, but worth restating:

1. **Defence in depth**: Istio AuthorizationPolicy (network) + `auth.service_acl` (application) + JWT scope check (claim) + `/authorize` (stateful) — every inter-service call is gated four ways.
2. **Observability is mandatory, not optional**: every service must `/health`, `/ready`, `/metrics`; emit structured JSON logs per Contract 6; propagate W3C trace headers. CI should enforce these via a base Helm chart template.
3. **Versioning everywhere**: APIs (`/v1/`), tools (`name@version`), skills (semver in YAML), schema migrations (Atlas), Kafka events (`schema_version` in envelope). Nothing in the platform is unversioned.
4. **Fail-mode is explicit, never implicit**: guardrails has `fail_mode: closed | open | circuit_break` per rule; service-to-service calls have circuit breakers (Istio DestinationRule); cancellation propagates via Kafka fan-out.
5. **Tenant isolation is architectural, not policy**: per-service PostgreSQL roles, RLS policies on every table, RLS-set per transaction. The platform must make cross-tenant access *impossible*, not just disallowed.

---

## 5. Recommended Order for First Cycle Implementation

The phases already prescribe an order; this is a reminder.

```
Week 1-2   Phase 0  Contracts (now 15 contracts; all ⚡ items)
Week 3-4   Phase 1  Infra: VPC → EKS → RDS/Valkey/MSK → Istio/Kong → add-ons → local-dev → CI/CD
Week 5-7   Phase 2  Auth: agent reg + JWT + JWKS rotation + service-tokens + /authorize basic
Week 5-7   Phase 3  LLMs: unified schema (with tool-use fields ready), 2 providers, streaming, usage events
                    ↳ parallel with Phase 2 once Auth's JWKS endpoint is reachable
Week 7-9   Phase 4  Guardrails: 6 input + 5 output rules, fail-mode, redaction format, streaming strategies
Week 9-12  Phase 9A xAgent first-cycle: pipeline engine (5 stages enabled), no tools/memory/skills
Week 12    SMOKE    Contract 15 — all 10 cases pass twice on cold-deployed dev environment

→ First cycle complete. Begin enhanced build (Phases 5, 6, 7, 8, then enable 📋 stages in Phase 9).
```

---

## 6. Files Touched

| File | Change Type |
|------|-------------|
| `phase-00-contracts.md` | Added Contracts 13 (Tenant), 14 (Migrations), 15 (Smoke Test); promoted 12 to ⚡; reorganised checklists; updated repo structure & exit criteria |
| `phase-01-infrastructure.md` | Added Component 17b (K8s add-ons), Component 17c (local dev story); expanded checklist |
| `phase-02-auth.md` | Added JWKS rotation procedure, layered Auth integration model, Component 8b (Service Token Issuance), service ACL table; expanded checklist |
| `phase-03-llms.md` | Rewrote Component 1 with tools/tool_calls/multi-modal/cost formula; added per-provider streaming usage and tool-call streaming rules; expanded checklist |
| `phase-04-guardrails.md` | Added output rules, ML classifier baselines, redaction format, Component 5b (fail-open/closed), Component 5c (streaming guardrails); expanded checklist |
| `phase-05-rag.md` | Per-dimension vector tables + HNSW index; pre-signed URL upload flow; Kafka-backed ingestion queue; expanded checklist |
| `phase-06-memory.md` | Per-dimension vector tables + HNSW; expanded checklist |
| `phase-07-tools.md` | MCP protocol naming clarification; versioned endpoints `/mcp/v1/`; tool versioning model |
| `phase-08-skills.md` | Platform-tenant skills-kb with documented cross-tenant-read exception; Component 4b template engine choice |
| `phase-09-xagent.md` | Scope boundary section; pipeline-based execution engine; corrected first-cycle checklist (no tools/memory/skills); ties to Contract 15 |
| `phase-10-a2a-orchestration.md` | Component 0 (A2A routing model); Component 5b (cancellation propagation) |
| `phases/README.md` | First-cycle path callout updated to be explicit about exclusions; smoke-test linkage |
| `phases/AUDIT_REPORT.md` | This file (new) |

---

## 7. Anything Else to Watch

- **The HLD/LLD distinction is not strictly preserved in every phase.** Most phases mix "what" (HLD) and "how" (LLD) in the LLD section. This is OK at this granularity but if more rigour is needed, the LLD sections could be split into "Components" (HLD-ish) and "Data Models & Internals" (LLD-ish).
- **Service architecture sub-planning (🏗️) is referenced in every phase but no template exists for what a "service architecture plan" should contain.** A short template would help — sections like: language/framework choice, internal module structure, dependency injection model, transaction boundaries, error-handling pattern, testing strategy. Worth adding as `contracts/templates/service-architecture-template.md` during Phase 0.
- **No mention of API rate-limit response headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`).** Contract 2 mentions `retry_after` in the body; standard headers are also expected by SDK consumers. Trivial to add in Contract 2 before SDKs are built.
- **Idempotency-Key handling has a contract (9) but no implementation detail** (Valkey key pattern, TTL, "did we already see this key?" check). Add a short implementation note when a service first needs it.

---

*End of original audit report. All originally-applied changes have been committed to the phase docs in-place.*

---

# Round 2 — External Operability Fix Log
> Date: 2026-05-25 | Reviewer: Claude (Opus 4.7) | Status: Applied

A second pass identified that the original audit hardened the platform for **internal** first-cycle delivery but left structural gaps in the "Externally Operable" principle. This round closes those gaps. Every change below has been **applied in-place** to the relevant phase doc.

## Cross-cutting (Phase 0 — Contracts)

| # | Fix | Where |
|---|-----|-------|
| R2.1 | **Contract 13 rewritten** to decouple `tenant_id` from `px0.org_id`. Introduced `source` enum (`px0-bridge | external-admin | self-serve-signup | sso-jit | manual-seed`); platform-neutral `cypherx.tenant.*` lifecycle events emitted by Auth regardless of source. External / self-hosted deployments are first-class. | `phase-00-contracts.md § Contract 13` |
| R2.2 | **Contract 1 issuer/audience configurable.** `iss` and `aud` resolve from `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE`, not literals. `deployment_id` claim added for federation. JWKS endpoint MUST be reachable externally. **OIDC discovery doc at `/.well-known/openid-configuration` is REQUIRED.** | `phase-00-contracts.md § Contract 1` |
| R2.3 | **Contract 2 — rate-limit / quota response headers pinned.** `X-RateLimit-*`, `X-Quota-*`, `Retry-After` mandatory on 429/402 AND on successful responses. New error codes `TENANT_SUSPENDED`, `EMBEDDING_DIM_MISMATCH`. | `phase-00-contracts.md § Contract 2` |
| R2.4 | **Contract 9 — idempotency implementation fully specified.** Key shape, request-fingerprint, in-flight collision, what's cached, Valkey-unavailability, TTL, SDK guidance. New error codes `IDEMPOTENCY_KEY_CONFLICT`, `IDEMPOTENCY_REQUEST_IN_FLIGHT`, `IDEMPOTENCY_NOT_SUPPORTED_FOR_ROUTE`. | `phase-00-contracts.md § Contract 9` |
| R2.5 | **Contract 12 — OIDC `client_credentials` Mode 3 added.** External services can hold a service identity via OAuth2 client_credentials grant against `/oauth/token`, either with `client_secret` or federated OIDC `client_assertion` (Sigstore / GitHub OIDC / cloud IAM). | `phase-00-contracts.md § Contract 12` |
| R2.6 | **Contract 18 — API Key & Resource ACL pattern (NEW).** Every SharedCore service implements the same `api_keys` + `api_key_acls` shape: per-key resource-level allowlists, Argon2id-hashed secrets, rotation with grace, exchange flow to JWT cached in Valkey. Per-service resource types enumerated. | `phase-00-contracts.md § Contract 18` |
| R2.7 | **Contract 19 — Usage Metering & Per-Tenant Quotas (NEW).** Every SharedCore service MUST emit `cypherx.<service>.usage.recorded` per billable op via outbox; never sampled. `auth.tenant_quotas` is canonical; per-window sliding counters in Valkey; quota breach → 429/402/413 with Contract 2 headers. | `phase-00-contracts.md § Contract 19` |
| R2.8 | **Contract 20 — External Onboarding (NEW).** Signup → email verify → tenant create (`source='self-serve-signup'`) → sandbox API key → upgrade-to-prod. Anti-abuse (captcha, disposable-email blocklist, risk score, IP rate limit). 30-day deletion grace. New error codes. | `phase-00-contracts.md § Contract 20` |
| R2.9 | **Contract 21 — Outbound Webhook Delivery (NEW).** Signed (HMAC-SHA256) HTTPS POSTs to subscriber URLs; replay-protected; exponential-backoff retries; replay endpoint; per-tenant rate-limited. External customers can subscribe to platform events without reading our Kafka. | `phase-00-contracts.md § Contract 21` |
| R2.10 | **Contract 15 — Smoke test extended** with 5 cases for external-developer scenarios (onboarding flow, idempotency replay + conflict, rate-limit headers, OIDC discovery). Now 15 cases total. | `phase-00-contracts.md § Contract 15` |

## Phase 2 — Auth

| # | Fix |
|---|-----|
| R2.11 | `auth.tenants` schema gained `source` enum (per Contract 13), `source_metadata` JSONB, `region`, `pending_deletion_at`. Emits the full `cypherx.tenant.*` event family. |
| R2.12 | **Component 1c — External Onboarding endpoints** (signup / verify / resend / upgrade / close-account); `auth.signup_attempts` with risk score; Turnstile captcha; disposable-email blocklist. |
| R2.13 | **Component 1d — Per-Tenant Quotas** (Contract 19): `auth.plan_defaults`, `auth.tenant_quotas`, admin + self-serve quota endpoints. |
| R2.14 | **Component 1e — Webhook subscriptions** (Contract 21): `platform.webhook_subscriptions` CRUD + `webhook-delivery` Deployment. |
| R2.15 | **Component 8b-ext — OAuth2 `client_credentials`** endpoint (`/oauth/token`); `auth.service_clients` + `auth.upstream_service_issuers` for federated identity. |
| R2.16 | **Component 8b-disc — OIDC discovery endpoint** (`/.well-known/openid-configuration`) per RFC 8414. |
| R2.17 | **Audit log read API** (`GET /v1/audit-log`, `/verify`, `/export`) — required for SOC 2 / HIPAA / GDPR. |

## Phase 3 — LLMs Gateway

| # | Fix |
|---|-----|
| R2.18 | **Semantic-cache cross-tenant leak FIXED.** Cache key now `llm-cache:{tenant_id}:{cache_epoch}:{model}:{hash}` — tenant_id is first segment; CI lint rejects keys without it. Sampling/output knobs included in hash. |
| R2.19 | `usage_records` gained `api_key_id` + `principal_type` for per-API-key billing dashboards. |
| R2.20 | **Dual auth mode**: external (bare JWT, no `X-Forwarded-Agent-JWT`) works alongside internal (service JWT + forwarded) — same downstream path. |
| R2.21 | **BYOK (Component 8) promoted to ⚡** — required for external SaaS day-one. AWS Secrets Manager + per-tenant KMS CMK + tenant-CRUD endpoint. |
| R2.22 | `cypherx.llms.usage.recorded` alias topic for canonical Contract 19 metering. `GET /v1/usage` + `GET /v1/cost` aggregations. |

## Phase 4 — Guardrails

| # | Fix |
|---|-----|
| R2.23 | **Component 5d — Per-tenant rate limit + per-check billing event** (NEW): `checks_per_min`, `input_bytes_per_min`, `custom_rules_max` from quotas; `cypherx.guardrails.usage.recorded` per check via outbox. |
| R2.24 | **Per-tenant redaction HMAC keys**: `guardrails.tenant_redaction_keys` table with `key_ref` to AWS Secrets Manager + per-tenant KMS CMK; platform key fallback; rotation endpoint. |
| R2.25 | **Component 6 — Policy Simulation ⚡ promoted**: persisted-policy + draft simulation; rate-limited per plan; never persists violations. |
| R2.26 | **Component 8 — Custom Tenant Rules ⚡ promoted**: `guardrails.rules.tenant_id` with mixed-scope RLS; tenant CRUD; ReDoS guard at write time; semantic-rule embeddings billed to LLMs usage. |

## Phase 5 — RAG

| # | Fix |
|---|-----|
| R2.27 | **Component 5c — KB ACL ⚡**: `rag.kb_acls` with `principal_type IN {agent, api_key, user, role, tenant}`; default tenant-wide ACL on KB creation; `private: true` opt-out. |
| R2.28 | **Component 5d — Usage metering ⚡**: `cypherx.rag.usage.recorded` per query/ingest/multimodal; `rag.pricing` + per-tenant override. |
| R2.29 | **Component 5e — Pluggable vector storage (interface ⚡)**: `IVectorStore` interface defined; `PgVectorAdapter` only first-cycle impl; `rag.tenant_backends` allows future Pinecone/Qdrant per-tenant. |
| R2.30 | `DELETE /v1/knowledge-bases/{kb_id}` promoted to ⚡. |
| R2.31 | Per-tenant storage quotas + queries/min + ingest/hour enforced from `auth.tenant_quotas`. |

## Phase 6 — Memory

| # | Fix |
|---|-----|
| R2.32 | **`principal` scope alias for `agent`** — generic identity type (agent_id, api_key_id, or app_id) so non-agent external products (chat apps) can use Memory. Legacy `scope=agent` rewritten at write time. |
| R2.33 | **`user_scope_visibility` default flipped to `principal_only`** for new tenants — fixes the silent cross-end-user leak risk for external chat-app vendors. Legacy `tenant_shared` opt-in via per-tenant flag. |
| R2.34 | **Component 7b — Usage metering ⚡**: `cypherx.memory.usage.recorded` per store/retrieve/extract/summarise/forget via outbox; `memory.pricing` table; cross-link with LLMs usage events via `request_id` for cost de-duplication. |
| R2.35 | **Component 7c — Pluggable vector storage (interface ⚡)**: same pattern as RAG. |
| R2.36 | **Component 7d — Optional per-user ACL** (📋 enterprise) overrides tenant-wide visibility flag. |
| R2.37 | Per-tenant memory quotas enforced from `auth.tenant_quotas`. |

## Phase 7 — Tools

| # | Fix |
|---|-----|
| R2.38 | **`registry.tenant_tools` table** with RLS — private tenant tools alongside platform tools. UNION discovery; tenant-resolution priority. |
| R2.39 | **External publisher submission flow** — `POST /v1/tenant-tools` with Trivy/Snyk image scan, mandatory `sandbox_class=gvisor` for non-platform tools, egress allowlist lint, `pending_review → active` lifecycle. Marketplace publish (cross-tenant) gated by `platform:admin` review. |
| R2.40 | **Component 1c — Capability Layer (NEW)**: `registry.capabilities` + `tool_capabilities` + `tenant_capability_bindings` so skills declare `required_capabilities` (portable) instead of hard-pinned tool names. |
| R2.41 | **Per-invocation billing event** `cypherx.tools.invocation.metered` on EVERY invocation (not only high-stakes); carries `publisher_tenant_id` + `consumer_tenant_id` for revenue-share. |
| R2.42 | Per-tenant tool quotas enforced (`private_tools_max`, `invocations_per_min`, `publishable_versions_max`). |
| R2.43 | `tools-public` namespace + NetworkPolicy isolating tenant marketplace tools from `shared-core`. |

## Phase 8 — Skills

| # | Fix |
|---|-----|
| R2.44 | **Component 4c — Tenant-private skills KB ⚡**: per-tenant `tenant-skills-{tenant_id}` KB auto-created on `cypherx.tenant.created`. Retrieval merges platform + tenant skills with reciprocal-rank fusion. |
| R2.45 | **External skill submission**: `POST /v1/skills/submit` with `publish_to: tenant | review | marketplace`; CI validates `required_capabilities` against tenant capability bindings. |
| R2.46 | **`required_capabilities` (preferred) + `action_type: capability`** replace hard-pinned `required_tools` — skill portability across publishers. |
| R2.47 | **`cypherx.skills.invoked` event ⚡ promoted** (was 📋) — per-execution billing with publisher/consumer tenant IDs. |
| R2.48 | Per-tenant skill quotas enforced. |

## Phase 9 — xAgent

| # | Fix |
|---|-----|
| R2.49 | **`RAG_QUERY` pipeline stage added** (📋 enhanced) — per-agent `allowed_kb_ids`, `rag_top_k_per_kb`, `rag_min_score` columns; iterates KBs, merges chunks into prompt; KB ACL enforced server-side by RAG. |
| R2.50 | **JWKS external path documented**: in-cluster URL is primary for internal services; PUBLIC JWKS at `{AUTH_ISSUER_URL}/.well-known/jwks.json` + signed bundle is the path for external A2A receivers. Earlier "never via ALB" guidance restricted to internal services only. |
| R2.51 | **Schema name disclosure note**: `xagent.agents` is implementation detail; external A2A interop uses platform-neutral API only. `platform.agents` view alias exposed by platform-service. |

## Phase 10 — A2A & Orchestration

| # | Fix |
|---|-----|
| R2.52 | **Cancel consumer-group rename** — neutral pattern `cypherx-agent-cancel-listener-<RUNTIME_KIND>-<POD_NAME>` where `RUNTIME_KIND=xagent` for the platform-built runtime and `external-<vendor>-<runtime>` for third-party A2A-compliant runtimes. External runtimes no longer inherit xagent's brand. |

## Phase 11 — Platform Management

| # | Fix |
|---|-----|
| R2.53 | **Billing Emitter abstraction (NEW)**: `IBillingEmitter` interface with concrete impls (`Px0BillingEmitter`, `StripeBillingEmitter`, `ChargebeeBillingEmitter`, `WebhookBillingEmitter`, `ManualInvoiceEmitter`). Per-tenant selection via `auth.tenants.source_metadata.billing_emitter`. Adding a backend = adding one class. |
| R2.54 | **Component 7 — Cross-Service Quota Enforcement (NEW)**: subscribes to `cypherx.*.usage.recorded` from every service; maintains `platform.tenant_running_totals` (per-tenant, per-window, per-meter); exposes `POST /v1/quotas/check` and emits `cypherx.tenant.quota.breached`. Catches cross-service aggregate overruns. |

## Phase 12 — Frontend

| # | Fix |
|---|-----|
| R2.55 | **Three deployment modes documented**: `bundled` (monolithic SPA, current), `per-service mini-UIs` (📋 — one Next.js per SharedCore service from monorepo + shared `@cypherx/admin-ui`), `headless / API-only` (no UI; external customers integrate via Admin REST APIs). Selected via `auth.tenants.source_metadata.frontend_mode`. |

## Phase 14 — SDKs

| # | Fix |
|---|-----|
| R2.56 | **Per-service SDK packaging**: each SharedCore service ships its OWN per-language package (e.g., `cypherx-llms`, `@cypherx-ai/memory`, …). Meta-package `cypherx-ai` / `@cypherx-ai/sdk` bundles all. Shared `cypherx-core` runtime package handles OIDC discovery, JWT, idempotency, retry, rate-limit back-off. Developers can pull only what they need. |

---

## Files Touched (Round 2)

| File | Change Type |
|------|-------------|
| `phase-00-contracts.md` | Contract 13 rewrite; Contract 1 (iss/aud configurable, OIDC discovery); Contract 2 (rate-limit headers); Contract 9 (idempotency impl); Contract 12 (OIDC client_credentials); NEW Contracts 18, 19, 20, 21; Contract 15 (5 new cases) |
| `phase-02-auth.md` | `auth.tenants` schema + lifecycle events; NEW Components 1c (onboarding), 1d (quotas), 1e (webhooks), 8b-ext (OAuth2), 8b-disc (OIDC discovery); audit-log read API |
| `phase-03-llms.md` | Cache key fix (tenant_id); per-API-key usage; dual auth mode; BYOK promoted ⚡; usage.recorded alias |
| `phase-04-guardrails.md` | Per-tenant redaction keys; NEW Component 5d (rate limit + billing event); Component 6 promoted ⚡ (simulation); Component 8 promoted ⚡ (custom tenant rules) |
| `phase-05-rag.md` | NEW Components 5c (KB ACL), 5d (usage metering), 5e (vector storage abstraction); DELETE KB promoted ⚡ |
| `phase-06-memory.md` | `principal` scope alias; `user_scope_visibility` default flipped; NEW Components 7b (metering), 7c (storage abstraction), 7d (per-user ACL) |
| `phase-07-tools.md` | `registry.tenant_tools` + publisher flow; NEW Component 1c (capability layer); per-invocation billing event; `tools-public` namespace |
| `phase-08-skills.md` | NEW Component 4c (tenant-private skills KB + external submission); `required_capabilities` + `action_type: capability`; `cypherx.skills.invoked` promoted ⚡ |
| `phase-09-xagent.md` | `RAG_QUERY` stage; `allowed_kb_ids` + `rag_top_k_per_kb` + `rag_min_score`; external JWKS clarification; schema name disclosure note |
| `phase-10-a2a-orchestration.md` | Neutral cancel consumer-group naming |
| `phase-11-platform-management.md` | Billing emitter abstraction; NEW Component 7 (cross-service quota enforcement) |
| `phase-12-frontend.md` | Three deployment modes (bundled / mini / headless) |
| `phase-14-sdks.md` | Per-service SDK packaging + shared `cypherx-core` runtime |
| `AUDIT_REPORT.md` | This Round 2 fix log |

---

## Round 2 — Build Readiness

After Round 2 fixes, every cross-cutting blocker from the original audit's §1 is addressed; every service-specific critical from §2 is closed. The platform now satisfies the "Externally Operable" principle at the contract, service, control plane, frontend, and SDK tiers.

**Remaining 📋 (deferred, not blocking external launch):**
- DR drills (Phase 13 already plans quarterly)
- Per-service mini-UIs implementation (architecture documented in Phase 12; build is Phase 13+)
- Pinecone / Qdrant adapters (interfaces shipped in Phase 5/6 first cycle; adapters land when first enterprise customer requires)
- A2A federation across customer trust domains (Phase 13 Domain 7 — already on the roadmap)
- Marketplace UI (Phase 12 mini-UIs / Phase 13 marketplace v1)

These are growth items, not pre-launch blockers.

*End of Round 2 fix log. All changes applied in-place; the original docs and the Round 2 edits are now the canonical specification.*
