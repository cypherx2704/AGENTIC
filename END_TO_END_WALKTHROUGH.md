# CypherX AI Platform — End-to-End Walkthrough

> **What this document is.** A ground-truth walkthrough of the entire `agentic/` monorepo, written from the actual source code, tests, database migrations, and contracts in this repository — not just from the planning docs. Where the design docs (`archive/Manoj/`) and the real code disagree, this document says so explicitly and follows the code. It covers every user-facing flow and every major feature across all services.
>
> **How it's organized.** §1–3 orient you. §4–7 are narrative walkthroughs of the four distinct ways a human or program actually touches this platform. §8 is a feature-by-feature reference per service. §9–10 cover the contracts that glue everything together and the test that proves it all works. §11–12 cover deployment and an honest "what's real vs. planned" assessment.

---

## Table of Contents

1. [What CypherX Is](#1-what-cypherx-is)
2. [System Map](#2-system-map)
3. [Four Ways In: Which Walkthrough Applies to You](#3-four-ways-in-which-walkthrough-applies-to-you)
4. [Walkthrough A — The CypherX Console (operator-facing agent platform)](#4-walkthrough-a--the-cypherx-console)
5. [Walkthrough B — The Developer / API-Only Path](#5-walkthrough-b--the-developer--api-only-path)
6. [Walkthrough C — cypherx-a1 (Engineering Memory Copilot)](#6-walkthrough-c--cypherx-a1-engineering-memory-copilot)
7. [Walkthrough D — the `demo/` Harness](#7-walkthrough-d--the-demo-harness)
8. [Feature Reference by Service](#8-feature-reference-by-service)
9. [Platform Contracts (the rules every service obeys)](#9-platform-contracts)
10. [The Canonical End-to-End Test (Contract 15)](#10-the-canonical-end-to-end-test-contract-15)
11. [Deployment & Environments](#11-deployment--environments)
12. [What's Real vs. What's Planned](#12-whats-real-vs-whats-planned)
13. [Source Map](#13-source-map)

---

## 1. What CypherX Is

CypherX AI is a **multi-tenant, language-agnostic agentic platform**: a set of independently deployable services that let a tenant (organization) register AI **agents**, give them tools/skills/knowledge, run tasks through them, and get back guardrailed, cited, auditable answers. Every service is designed to also work as a **standalone SaaS product** — the LLM gateway, the guardrails service, the memory store, and the RAG engine are each sellable on their own, not just internal plumbing.

Two protocols run through everything:

| Protocol | Purpose |
|---|---|
| **A2A** (Agent-to-Agent) | Standardized task delegation, status callbacks, and streaming between agents |
| **MCP** (Model Context Protocol) | Standardized interface for agents to discover and invoke Tools and Skills |

The platform is **not** a single product — it's a foundation that currently carries **two** end-user-facing products (the general-purpose CypherX Console, and the flagship cypherx-a1 engineering-memory copilot), plus a full developer/API surface for anyone building their own agents on top.

---

## 2. System Map

### 2.1 Every component, what it does, and its status

| Component | Path | What it is | Stack | Status |
|---|---|---|---|---|
| **px0** | *(external — not in this repo)* | Existing company-wide identity/org/billing (Stripe)/notifications/audit for **human users**. CypherX integrates with it but doesn't own it. | Kotlin/Spring/Next.js | External, pre-existing |
| **Auth** | `Shared Core/auth` | Issues and validates **agent** JWTs (not human JWTs), service tokens, API keys; owns tenant lifecycle, onboarding, webhooks, audit log | Kotlin/Spring Boot | Implemented (~150 files) |
| **LLMs Gateway** | `Shared Core/llms` | The only path to an LLM provider anywhere in the platform | Python/FastAPI | Implemented through WP06 |
| **Guardrails** | `Shared Core/guardrails` | Input/output safety checks, PII redaction, policy engine | Python/FastAPI | Implemented (~155 tests) |
| **Memory** | `Shared Core/memory` | Long-term, principal-scoped agent memory (pgvector) | Python/FastAPI | Implemented (first-cycle scope) |
| **RAG** | `Shared Core/rag` | Tenant knowledge bases, ingestion, hybrid retrieval | Python/FastAPI | Substantially implemented |
| **xAgent / ax-1** | `xAgent/ax-1` | The agent **execution runtime** — runs one agent's task through a staged pipeline | Python/FastAPI | Built, 27 test files |
| **xAgent / ax-2** | `xAgent/ax-2` | Future A2A router + multi-agent orchestrator | Python/FastAPI (planned) | **Empty placeholder** — only a `CLAUDE.md` |
| **Tool Registry** | `Tools/tool-registry` | Catalogue + health-tracking of MCP tool servers | Python/FastAPI | Implemented |
| **tool-web-search** | `Tools/tool-web-search` | A concrete MCP tool (`web_search`) | Python/FastAPI | Implemented |
| **Skill Registry** | `Skills/skill-registry` | Catalogue of declarative skill definitions | Python/FastAPI | Implemented (mirrors tool-registry) |
| **cypherx-a1** | `CoreProjects/cypherx-a1` | A **sibling product** to xAgent — an engineering-knowledge graph + RAG copilot that ingests GitHub/Jira/Slack | Python/FastAPI | Implemented (MVP + Phase A/B) |
| **mcp-eng-memory** | `CoreProjects/cypherx-a1/mcp-eng-memory` | Stateless MCP facade in front of cypherx-a1, for AI coding agents/IDEs | Python/FastAPI | Implemented |
| **Frontend app** | `frontend/app` | The CypherX Console (Next.js SPA) | Next.js/TypeScript | Implemented, 17 screens |
| **Frontend BFF** | `frontend/bff` | Backend-for-Frontend: sessions, CSRF, proxying, SSE relay | Node/Fastify | Implemented |
| **Frontend demo** | `frontend/demo` | Zero-dependency internal smoke-test harness | Python stdlib | Implemented (prototype) |
| **Platform** | `platform/` | Intended control plane (service registry, config, billing rollup, deploy/rollback) | Kotlin/Spring (planned) | **Stub — GitLab boilerplate only** |
| **contracts** | `contracts/` | The 21 versioned contracts every service is built against | OpenAPI/JSON Schema/Markdown | Implemented, actively enforced |
| **infra** | `infra/` | Terraform/Terragrunt IaC + Docker Compose local dev stack | Terraform, Helm, Compose | Implemented (IaC only — no AWS resources actually applied yet) |
| **gitops** | `gitops/` | ArgoCD App-of-Apps definitions | ArgoCD YAML | Scaffolding — roots exist, zero service child-apps yet |
| **charts** | `charts/` | The shared `cypherx-service` base Helm chart | Helm 3 | Implemented |
| **archive** | `archive/` | Planning/spec repo (`Manoj/`) — master plan, phase specs, contracts rationale | Markdown | Design source, no code |

### 2.2 Local port map (from `infra/compose/docker-compose.yml`)

| Service | Host port | Notes |
|---|---|---|
| Edge (Caddy) | `:8000` | Single entry point — `/` → SPA, `/bff/*` → BFF, `/api/<svc>/*` → service |
| auth | `:8080` | |
| frontend-app | `:3000` | Next.js dev/build server |
| xagent (ax-1) | `:8083` | |
| llms-gateway | `:8085` | |
| guardrails | `:8086` | |
| rag | `:8087` | |
| memory | `:8088` | |
| tool-registry | `:8089` | |
| demo | `:8090` | Opt-in, `--profile demo` |
| tool-web-search | `:8091` | |
| frontend-bff | `:8092` → 8088 in-container | |
| cypherx-a1 | `:8093` | |
| mcp-eng-memory | `:8094` | |
| skill-registry | `:8095` | |

Backing infra: Postgres is **external Neon** (no local Postgres container in `infra/compose`), Kafka is **Redpanda**, cache is **Valkey**, object storage is **MinIO**. Every app service listens on `8080` in-container by convention; `/livez` is process-only, `/readyz` checks real dependencies.

### 2.3 The one relationship that's easy to get wrong

**cypherx-a1 is not "part of" xAgent — it's a peer.** Both are described in cypherx-a1's own docs as *"consuming apps"* that sit on top of the same SharedCore services (auth, llms, guardrails, rag, memory). xAgent/ax-1 is the platform's **general-purpose agent runtime**; cypherx-a1 is a **specific product** (an engineering-memory copilot) that currently calls the LLMs gateway and Guardrails directly rather than routing through xAgent — a deliberate, documented decision (ADR-003) because xAgent doesn't yet model cypherx-a1's hybrid graph+RAG retrieval. The plan is for cypherx-a1 to eventually become an A2A **caller** of xAgent, but that hasn't happened yet.

---

## 3. Four Ways In: Which Walkthrough Applies to You

| If you are... | You experience... | See |
|---|---|---|
| A tenant admin managing agents, watching costs, editing guardrail policies | **The CypherX Console** — a Next.js admin/ops UI | [Walkthrough A](#4-walkthrough-a--the-cypherx-console) |
| An external developer or SDK user calling the platform's REST APIs directly | **The API surface** — `/v1/onboarding`, `/v1/tasks`, `/v1/chat/completions`, webhooks | [Walkthrough B](#5-walkthrough-b--the-developer--api-only-path) |
| An engineer asking "who owns this service" or an AI coding agent looking up context mid-task | **cypherx-a1's copilot** (web console or MCP server) | [Walkthrough C](#6-walkthrough-c--cypherx-a1-engineering-memory-copilot) |
| A CypherX engineer verifying the guardrail/LLM spine works locally | **The `demo/` harness** | [Walkthrough D](#7-walkthrough-d--the-demo-harness) |

These are genuinely separate experiences with separate UIs — this doc treats each as its own story rather than forcing them into one flow.

---

## 4. Walkthrough A — The CypherX Console

This is the Next.js app at `frontend/app`, served through the BFF at `frontend/bff`. **Important correction to the repo's own docs:** `frontend/CLAUDE.md` describes an old "platform-credential" login (tenant ID + agent ID + admin API key). The actual shipped code replaced this entirely with email/password + Google OAuth + self-serve registration. This walkthrough follows the real code.

### 4.1 Before you have an account

There is **no marketing/landing page** in this app — `/` is the authenticated dashboard, gated by an auth check. A brand-new visitor's first screen is always `/login`.

- **`/login`** — email + password fields, a "Continue with Google" link (a real page navigation to let Google's consent screen render, not an AJAX call), a link to `/register`. Supports `?next=` (relative-only, open-redirect-guarded, so a mid-session expiry can bounce you back to where you were) and `?error=google`.
- **`/register`** — workspace name (optional), email, password (≥8 chars). Submitting **immediately and synchronously** provisions a tenant, a user, an orchestrator agent, and a first API key — then auto-logs you in. The success screen shows that API key **once**, with a "Continue to console" button.
- **`/register/verify`** — landing page for an email-verification link (`?token=`). This is the back half of a *second*, more gated onboarding funnel (Contract 20) that supports resend-with-backoff and anti-enumeration — but as of this snapshot, **no page in the app actually starts that funnel** (no signup form calls it). In practice, every real user today goes through `/register`, not the email-verification path. See [§9.6](#96-onboarding-contract-20) for the full designed funnel, which *is* live at the API level even though the UI for the first half doesn't exist yet.

### 4.2 Signing in

Three ways, all mediated entirely by the BFF (the browser never sees a JWT):

1. **Email + password** — `POST /bff/login` → BFF calls Auth's `POST /v1/auth/login` → BFF opens a session.
2. **Google OAuth** — `/bff/auth/google` → 302 to Google → `/bff/auth/google/callback` exchanges the code server-side → session opened → redirect to `/`.
3. **Self-serve register** — described above; auto-logs in immediately after provisioning.

**What "session" means here:** the BFF generates a 256-bit opaque session id, AES-256-GCM-seals a JSON record (`tenantId, agentId, scopes, downstreamToken, csrfToken, expiresAt`) with a server-held key, and stores it in Valkey. Two cookies go to the browser:

| Cookie | httpOnly | Contains |
|---|---|---|
| `cypherx_sid` | Yes | Only the opaque session id — JS can never touch it |
| `cypherx_csrf` | No | A CSRF token, also bound inside the encrypted session |

Every mutating request (`POST/PUT/PATCH/DELETE`) must present `X-CSRF-Token` matching both the cookie and the value sealed inside the session, checked with a timing-safe comparison — any mismatch is `403 CSRF_FORBIDDEN`. If a request ever comes back `401` (session expired), the app shows a toast and redirects to `/login?next=...`.

### 4.3 Inside the console: shell & navigation

Once authenticated, every screen shares one shell: a sticky top bar (logo, current tenant id, sign-out) and a left sidebar with **15 sections**, in this order:

```
Dashboard → Agents → Orchestrator → Approvals → API Keys → Task Runner →
Task Feed → Guardrails → LLM Connections → LLM Aliases & Rules → LLM Usage →
Knowledge Bases → Audit Log → Tenant → Platform Health
```

The **Dashboard** (`/`) is a grid of 9 shortcut tiles into the sections below, plus your session's granted scopes shown as badges — copy on the page states the design philosophy plainly: *"Everything routes through the BFF — the browser never holds a token."*

### 4.4 Building your first agent

**Agents** (`/agents`) lists your tenant's agents in a table (name, agent id, status, allowed scopes, created date). "New agent" opens a modal for a name and comma-separated scopes (default `agent:execute, llm:invoke, guardrails:check`), then redirects to the agent's detail page — the **Agent Builder**:

| Field | What it controls |
|---|---|
| Status | `pending_config` / `active` / `inactive` |
| Model | Populated from the LLMs gateway's `/v1/models`, plus `smart`/`fast` aliases |
| Memory scope | `none` / `agent` / `user` / `tenant` / `session` |
| System prompt | Free text |
| Max tokens, temperature, token budget per task | Numeric limits |
| Guardrail policy id | Which policy (§4.11) applies to this agent |
| Allowed tools / skills / KB ids | Comma-separated — gates what the agent can reach |
| RAG top-k / min-score | Retrieval tuning, if KBs are allowed |

Saving is **two explicit steps**: "Save config" persists the form without changing status; "Publish" saves *then* flips status to `active` — and if step 2 fails after step 1 succeeded, the button becomes "Retry publish (step 2)" so you never have to resubmit the whole form.

### 4.5 Issuing an API key

**API Keys** (`/keys?agent=<id>`) — a table of keys (prefix, name, scopes, status, last-used) with "New key" and "Revoke" (behind a confirm dialog). A freshly issued key is shown exactly once, in a modal you can't dismiss by clicking outside it, with an explicit "I have stored it" button — because the raw secret cannot be retrieved again afterward.

### 4.6 Running a task — the core loop

**This console is an operator/ops tool, not a chat product.** There's no persistent multi-turn conversation UI anywhere. The closest thing to "talking to the agent" is **Task Runner** (`/tasks/run`), and it's a single-shot submit-and-watch loop:

1. Pick an Agent ID, type a message, leave "Stream the timeline live (SSE)" checked, click **Run task**.
2. This calls `POST /bff/api/xagent/v1/tasks { agent_id, input: { message } }`, proxied by the BFF straight through to xAgent's `POST /v1/tasks`.
3. The right-hand panel opens a browser `EventSource` against `GET /v1/tasks/{id}/stream` (riding the same session cookie — no token ever touches JS) and renders a live **timeline**: one dot per pipeline stage, colored green/red/amber as each completes.
4. When the task finishes, you see the answer, and three stats: tokens used, cost, step count.

Two placeholder prompts in the UI copy hint at the guardrail system on purpose: *"try a prompt-injection to see the 422 block"* — submitting a jailbreak attempt or a message containing an email address is meant to visibly demonstrate the block/redact behavior described next.

### 4.7 What actually happens inside a task

This is the heart of the whole platform. When you submit a task, xAgent's `agent_runtime` service runs it through a **named, independently feature-flagged pipeline** (`core/pipeline.py`). The first-cycle default order is:

```
LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → EVENT
```

| Stage | What happens |
|---|---|
| **LOAD** | Resolves your agent's saved config (Valkey-cached, DB fallback); `409` if the agent isn't `active` |
| **PRE_GUARDRAIL** | Sends your message to Guardrails' `/v1/check/input`. `allow`/`warn` → continue. `redact` → your message is silently rewritten (e.g. an email becomes `[REDACTED:email:abc123]`) before the model ever sees it. `block` → the pipeline stops immediately with `422 GUARDRAIL_VIOLATION` — **the LLM is never called** |
| **PROMPT_BUILD** | Assembles the system + user messages; if RAG/Memory/Skills context is enabled for this agent, splices it in as a second system message, trimmed to a **prompt budget** (see below) |
| **LLM** | One call to the LLMs gateway's `/v1/chat/completions`, capped at `min(your max_tokens, agent's token_budget_per_task)` |
| **POST_GUARDRAIL** | Checks the model's reply the same way — can redact leaked PII or block the answer entirely |
| **EVENT** | Always runs, no matter what happened above (success, block, timeout, cancel, crash) — finalizes the task and atomically writes both the DB row and a Kafka event in one transaction, so they can never disagree |

A clean run writes exactly 3 audit steps: `guardrail_check_input`, `llm_call`, `guardrail_check_output` — visible in the Task Runner's timeline and on the persisted Task Detail page afterward.

**Optional stages** (flagged off by default, and additionally a per-agent opt-in even when the flag is on): `RAG_QUERY` (queries every KB in the agent's `allowed_kb_ids`, drops chunks below `rag_min_score`), `MEMORY_RETRIEVE`/`MEMORY_WRITE` (governed by the agent's `memory_scope`), `TOOL_LOOP` (lets the model call MCP tools mid-turn, metering each call), `SKILL_LOAD` (splices permitted skill names/descriptions into the prompt — it does **not** execute the skill's steps itself).

**The prompt budget, concretely:** `char_budget = token_budget_per_task × 0.30 × 4` (a cheap 4-chars-per-token estimate). If the assembled RAG+memory+skill context exceeds that, whole items are dropped — never truncated mid-item — in a fixed order: RAG chunks first, then memories, skills last.

**Reliability details worth knowing:**
- **Idempotency:** send an `Idempotency-Key` header and a retried request with the same body replays the original response (`Idempotent-Replayed: true`) instead of re-running; a different body with the same key is a `409`; if the idempotency store is unreachable, the request is rejected (`503`) rather than silently risking a duplicate run.
- **Cancellation:** `DELETE /v1/tasks/{id}` is cooperative — it sets a signal the pipeline checks *between* stages, so an in-flight LLM call finishes before a cancel takes effect.
- **Async mode:** `POST /v1/tasks?mode=async` (requires an idempotency key) returns `202` immediately and streams the same way via SSE.
- **The sweeper:** a background job that finds tasks stuck mid-run (e.g. from a crash) past a grace period and force-finalizes them as `failed`, so nothing hangs forever in the Task Feed.

### 4.8 Watching it live: SSE streaming

The BFF's proxy has one special case: `GET xagent/v1/tasks/{id}/stream` is detected and relayed **byte-for-byte, unbuffered** (it sets `X-Accel-Buffering: no` and pumps chunks as they arrive) rather than going through the normal buffered proxy path. The browser's `EventSource` sees plain SSE frames (`snapshot`, `step`, `done`, `error`, `content_filter`) riding its ordinary session cookie — the downstream agent token is only ever used on the BFF→xAgent leg, never sent to the browser.

### 4.9 Reviewing history: Task Feed & Task Detail

**Task Feed** (`/tasks`) — a live-polling (5s) table of every task, filterable by status/agent/time, with a pause/resume toggle. **Task Detail** (`/tasks/[id]`) — the full record: status, tokens/cost/duration/step-count, the answer, and the same step-by-step timeline component used in Task Runner.

### 4.10 Shaping safety: Guardrails policies

**Guardrails** (`/guardrails`) has two halves. The **policy editor**: name, stream mode (`buffer`/`passthrough`), a fail-mode override (`closed`/`open`/inherit), and a checklist of every rule in the platform's rule catalog with per-rule enable + action-override. The platform default policy is read-only; tenant policies are yours to edit. Below it, a **violation log** — explicitly captioned "redaction-safe" because the "matched" field shows only tokens/truncations, never raw PII.

### 4.11 Bringing your own LLM keys

**LLM Connections** (`/llms`) is the **BYOK** screen: connect your own provider key (presets for OpenRouter/OpenAI/Anthropic/Together/Groq/Custom), stored encrypted against your tenant — the copy is explicit that "there is no platform fallback" once you remove a key that other config depends on. **LLM Aliases & Rules** (`/llms/aliases`) lets you map friendly names (e.g. `fast`, `smart`) to real model+provider combos, and set allow/block rules per provider+model, including a "billing bypass" flag.

### 4.12 Watching spend: Usage & Cost

**LLM Usage** (`/usage`) — stat tiles (tokens, cost, requests, cache hit/write breakdown) plus two bar charts and detail tables, groupable by date/model/agent/provider.

### 4.13 Grounding answers: Knowledge Bases (RAG)

**Knowledge Bases** (`/rag`) lists your KBs (doc/chunk counts, embedding model, status) and includes a "Test query" sandbox — pick a KB, type a query, see the retrieved chunks with similarity scores, without needing to run a whole agent task.

### 4.14 Proving nothing was tampered with: Audit Log

**Audit Log** (`/audit`) — filterable event table, plus a genuinely distinctive feature: a **"Verify chain"** button that calls `GET /v1/audit-log/verify`, which walks a per-tenant SHA-256 hash chain over every audit row and reports either "Chain intact — N rows verified" or exactly which row broke and why. This is real tamper-evidence, not just an audit trail.

### 4.15 Checking your limits: Tenant & Quotas

**Tenant** (`/tenant`) — read-only view of your tenant id, granted scopes, and the *effective* resolved quota limits per service (agents_max, requests_per_min, kbs_max, etc.) — overrides happen out-of-band by a platform admin, not from this screen.

### 4.16 Watching the platform itself: Platform Health

**Platform Health** (`/health`) — an auto-refreshing grid, one card per upstream service, showing `/livez`/`/readyz` status, sourced from the BFF's own health-fanout prober rather than proxying each service individually.

### 4.17 Going multi-agent: Orchestrator & sub-agents

**Orchestrator** (`/orchestrator`) explains that your tenant's orchestrator agent is the only one that can create sub-agents, which inherit a subset of its scopes. You can create a sub-agent by picking which of the orchestrator's own scopes to grant it (minus `orchestrator:manage` itself), and deactivate sub-agents from a table. If your signed-in agent lacks `orchestrator:manage`, creation is disabled with a warning banner.

### 4.18 Putting a human in the loop: Approvals

**Approvals** (`/hil`) — set the orchestrator's mode (`automated` / `human_in_loop` / `partial`, with per-trigger toggles for `tool_execution`, `sub_agent_creation`, `llm_restriction`, `skill_execution`), and a queue of pending approval requests with **Grant**/**Deny** buttons. This is the UI on top of Contract 16's step-up approval-token model (§9.7).

---

## 5. Walkthrough B — The Developer / API-Only Path

Everything the Console does, a developer can also do directly against the REST APIs — this is the path an external SaaS customer or an SDK actually takes, and it's designed to work **without ever touching px0**.

### 5.1 Becoming a tenant (external onboarding, Contract 20)

```
1. POST /v1/onboarding/signup {email, full_name, intended_use, terms_accepted_version}
   → Auth records a pending signup, emails a verification link (24h TTL)

2. GET /v1/onboarding/verify?token=...
   → creates the tenant (source='self-serve-signup'), seeds free-tier quotas,
     emits cypherx.tenant.created (every SharedCore service bootstraps its
     tenant_config off this event), mints a session JWT

3. POST /v1/api-keys  (with that session JWT)
   → returns a sandbox key: cx_sandbox_auth_...
   → quickstart: curl -H 'Authorization: Bearer cx_sandbox_...' \
                 https://sandbox.cypherx.ai/v1/chat/completions

4. POST /v1/onboarding/upgrade {billing_method: stripe|px0|manual-invoice, ...}
   → plan flips free → pro, cypherx.tenant.plan_changed fires,
     you can now mint cx_prod_... keys
```

Anti-abuse is built in: disposable-email blocklist, 10 signups/hour/IP, captcha, and a risk-score heuristic that routes suspicious signups to manual review. Sandbox tenants run in an isolated cluster and **auto-purge after 7 days** — nothing you do there touches production data. A separate **SSO-JIT** path exists for enterprise customers: a first successful login from a pre-configured IdP silently provisions the tenant, skipping email verification since the IdP already vouched for identity.

### 5.2 Getting an agent JWT

Two ways to authenticate as a service (not a human):

- **Internal/first-party:** each service authenticates itself to Auth with a per-service bootstrap secret and gets a 5-minute service JWT, renewed continuously in the background.
- **External:** OAuth2 `client_credentials` grant against `POST {AUTH_ISSUER_URL}/oauth/token` — register a service client, exchange `client_id`/`client_secret` for a short-lived token. Discovery is fully OIDC-standard: `GET {AUTH_ISSUER_URL}/.well-known/openid-configuration` and `/.well-known/jwks.json`.

### 5.3 Calling the platform directly

- `POST /v1/chat/completions` (LLMs gateway) — the same unified schema the Console's agents use under the hood, streaming or not.
- `POST /v1/tasks` (xAgent) — submit a task exactly like the Console's Task Runner does; get back a Contract-3-shaped response with `task_steps`, `tokens_used`, `cost_usd` always populated.
- `POST /v1/kbs/{id}/documents` / `POST /v1/kbs/{id}/query` (RAG) — ingest and query knowledge bases directly.
- `POST /mcp/v1/invoke` on any tool server (e.g. `tool-web-search`) — invoke a tool the same way xAgent's tool loop does.

### 5.4 Getting notified asynchronously (webhooks, Contract 21)

Since external customers can't read the platform's internal Kafka topics directly, tenant-scoped webhook subscriptions deliver the **full event envelope** over HTTPS, HMAC-SHA256 signed, at-least-once, with exponential backoff over ~3 days and a 30-day replay endpoint. Example subscribable event types: `cypherx.llms.usage.recorded`, `cypherx.tenant.plan_changed`.

### 5.5 SDKs

Planned (Python P1, TypeScript P1, Go P2) but explicitly deferred until the APIs stabilize — not present in this repo yet.

---

## 6. Walkthrough C — cypherx-a1 (Engineering Memory Copilot)

cypherx-a1 (product name **"Autonomous Engineering Memory"**) is a separate, flagship application: it continuously ingests an organization's engineering history and turns it into a queryable, cited memory — replacing the "manually-maintained wiki" with something that reads the systems of record instead.

### 6.1 Ingesting your engineering history

Today the connector is **GitHub** (commits, PRs, reviews, issues) — Jira and Slack are designed-for but not built. Two entry points feed the same pipeline:

- `POST /v1/connectors/{kind}/sync` (authenticated, full sync) — for scheduled/manual syncs
- `POST /webhooks/{kind}?tenant=<uuid>` (HMAC-signed, real-time) — graph-only, since there's no agent identity on a webhook call to embed into RAG under

```
GitHub API/webhook → canonical records (nodes, edges, docs)
   → LAND (idempotent raw-event log)
   → NORMALIZE (upsert into the knowledge graph; resolves one person's
                GitHub login / Slack uid / email onto one canonical identity)
   → RAG INGEST (chunks land in one of 4 fixed per-tenant knowledge bases:
                 eng-code, eng-conversations, eng-docs, eng-incidents)
   → LINK (citations wired back from RAG chunks to graph entities, in the
           same transaction as the outbox event — never allowed to diverge)
```

A separate, on-demand `POST /v1/extract` pass runs an LLM over PRs/tickets/incidents/decisions to mine **semantic edges** (`depends_on`, `caused`, `resolved`, `expert_in`, `decided_in`) that plain ingestion can't see syntactically — every extracted edge is confidence-scored and bitemporal (a later extraction can supersede an earlier one without destroying history).

### 6.2 Asking it a question

`POST /v1/copilot/ask` runs a 7-stage pipeline that deliberately mirrors xAgent's own shape (LOAD→GUARDRAIL→...→GUARDRAIL→EVENT), even though it currently bypasses xAgent itself:

```
recall relevant memory → guardrail-check the question → hybrid retrieve
   → build a "only answer from context, cite specifics, say 'I don't know'
     otherwise" prompt → call the LLM → guardrail-check the answer
   → store an episodic memory of the exchange → return a cited answer
```

**Hybrid retrieval** fuses three legs — a graph full-text search, a Postgres keyword search, and RAG dense-vector search — via Reciprocal Rank Fusion, and every RAG chunk hit is resolved back to its source graph entity so the two legs reinforce each other's confidence. Answers are **never uncited** — every claim traces back to a PR, ticket, incident, or commit.

### 6.3 Asking it deterministically, with no LLM at all

Five (six, counting a newer activity-timeline endpoint) endpoints answer the same class of question with pure graph traversal — no model call, no hallucination risk:

| Question | Endpoint | How |
|---|---|---|
| Who owns X? | `/v1/graph/who-owns` | Reverse ownership-edge aggregate, ranked by confidence |
| What breaks if I change X? | `/v1/graph/what-breaks` | Recursive `depends_on` traversal, bounded by `max_hops` |
| Who are the experts on T? | `/v1/graph/experts` | Topic search → ownership-signal aggregate |
| Why was X built? | `/v1/graph/why-built` | Full-text search over PRs/decisions/tickets |
| What's adjacent to X? | `/v1/graph/neighbors` | One-hop typed traversal, both directions |
| What changed? | `/v1/graph/activity` | Time-ordered timeline of changes/PRs/tickets/incidents |

### 6.4 Where you'd actually use this

- **The Console UI** at `:8093/ui` — an "Ask" box, the graph-query lenses above, an activity timeline, entity detail pages, and a citations rail.
- **The MCP server** (`mcp-eng-memory`, `:8094`) — a stateless facade with **8 tools** (`who_owns`, `why_built`, `what_breaks_if_changed`, `experts_on`, `graph_neighbors`, `what_changed`, plus two LLM-backed ones, `incident_root_cause` and `how_does_x_work`) that any MCP-speaking coding agent or IDE can call mid-task, before making a change. It re-implements zero business logic — every tool call is a thin, authenticated proxy onto the exact same service that backs the human UI ("there is no second brain, only a second door"), and it's kept as its own deployable process specifically for blast-radius isolation.

---

## 7. Walkthrough D — the `demo/` Harness

`frontend/demo` is **not the product** — it's a ~360-line, dependency-free Python (`http.server`) prototype that predates the real Console, used by platform engineers to manually exercise the guardrail→LLM→guardrail spine against the raw services (auth, xagent, llms, guardrails) with zero setup. It binds to `127.0.0.1` by default because it's explicitly unauthenticated. Its single page has a live health strip, an agent info card, and a "Run a task" box with three canned quick-chips: **Happy path** ("What is 2+2?"), **Prompt injection (blocked)**, and **PII email (redacted)** — the same three canonical scenarios the platform's own smoke test formalizes (§10). This is best understood as the rough draft the real Task Runner screen (§4.6) grew out of.

---

## 8. Feature Reference by Service

### 8.1 Auth (`Shared Core/auth`) — Kotlin/Spring Boot

The system of record for tenants and agent identity. **Not** end-user auth (that's px0).

- Mints RS256 agent JWTs, service tokens, OAuth2 `client_credentials` tokens, and API keys — one canonical minting path (`JwtMintService`); HS256 is forbidden
- Serves `/.well-known/jwks.json` and OIDC discovery for every other service
- `POST /v1/authorize` — the platform's RBAC decision endpoint
- Live revocation: `POST /v1/tokens/revoke` (single token) and `.../revoke-all-tokens` (kill-switch)
- Tamper-evident append-only audit log with a cryptographic hash-chain verify endpoint
- Tenant lifecycle, per-tenant quotas, self-serve onboarding, webhook delivery worker
- ~18 REST controllers in total; signing keys are envelope-encrypted and never exist as a raw env var

### 8.2 Guardrails (`Shared Core/guardrails`) — Python/FastAPI

Sits between the agent runtime and the LLM gateway on both the way in and the way out.

- `POST /v1/check/input`, `POST /v1/check/output` — always return `200`; the *caller* (xAgent) turns a `block` decision into a `422`. Precedence: **BLOCK > REDACT > WARN > ALLOW**
- PII redaction (regex + optional Presidio), each match becomes a deterministic `[REDACTED:cat:hex8]` token — raw PII never leaves the pipeline
- Prompt-injection/jailbreak defense, with "spotlighting" for untrusted spans (e.g. RAG/tool output)
- Groundedness checking (heuristic or LLM-backed), 11 built-in rules + tenant custom rules
- Fail-**closed** on a rule timeout (blocks by default); policy-cache/redaction-key lookups are fail-**open**
- Classifier cascade: keyless `stub` default → optional `detoxify` → remote `llms_gateway` classify for uncertain cases

### 8.3 LLMs Gateway (`Shared Core/llms`) — Python/FastAPI

The single choke point every other service routes model calls through — nothing else talks to a provider SDK directly.

- `POST /v1/chat/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/classify`
- Tool-call emulation for models without native tool support (prompt-injects a schema + protocol, parses the reply back)
- BYOK: per-tenant provider keys, sealed with AES-256-GCM, disabled entirely if the platform KEK is unset
- Per-API-key ACLs restricting which models/providers a key may use
- Billing: a gateway-minted `llm_call_id` is the uniqueness key for every cost event, written transactionally — journaled to disk for replay if the DB write fails, so a client is never double-charged or silently under-billed
- `MOCK_PROVIDERS=true` gives a fully keyless, deterministic local/test mode

### 8.4 Memory (`Shared Core/memory`) — Python/FastAPI

Durable, principal-scoped agent memory on pgvector.

- `POST /v1/memories` (idempotent, dedups near-duplicates ≥0.95 cosine similarity instead of inserting a copy), `/v1/memories/search`, by-id CRUD, `/v1/sessions`, `/v1/gdpr/wipe`
- Scope model: `principal_only` (hard cross-user leak guard, no policy can override it) vs. `tenant_shared`
- By-id lookups return **404, never 403**, for memories you can't see — avoids leaking existence
- Relevance scoring, contradiction detection, and consolidation are all built but flag-gated **off** by default (pure cosine similarity is the current default)

### 8.5 RAG (`Shared Core/rag`) — Python/FastAPI

Universal retrieval-augmented generation, used by both xAgent and the Skills system.

- KB CRUD; the embedding model+dimension is resolved and **frozen at creation**
- `POST /v1/kbs/{id}/query` — `dense` (default) / `hybrid` (dense+lexical via RRF) / `sparse`, optional cross-encoder reranking (flag-gated off by default)
- Inline ingestion (≤100 KiB) and a presigned-upload + async worker path (≤100 MiB) for larger documents
- ACL-filtered retrieval — a KB with zero matching ACL rows is readable by **nobody**, no fallback
- A special cross-tenant "platform-read" mode exists solely so the Skills system can read the shared `platform-skills` KB

### 8.6 xAgent / ax-1 — Python/FastAPI

Covered in depth in [§4.7](#47-what-actually-happens-inside-a-task). Additional internals worth knowing:

- **A2A today is just the response *shape*** (Contract 3) — nothing in ax-1 lets one agent call another yet. A hard rule (`body.agent_id` must equal the caller's own JWT `agent_id`) explicitly blocks cross-agent invocation until Phase 9B/ax-2 exists.
- **ax-2 is genuinely empty** — just a `CLAUDE.md` describing the planned A2A router (consistent-hash routing to the right `ax-1` pod) and a DAG-based Orchestrator, gated to not even start until ax-1 passes the smoke test twice plus 7 clean days in staging.
- Authorization decisions are cached in Valkey; a cached **deny** skips the network call to Auth entirely, but Auth 5xx/timeouts **fail open** ("availability wins; the JWT was already verified").

### 8.7 Skills & Tools Registries

**The vocabulary, precisely:** a **Tool** is a live MCP server the runtime calls over HTTP at execution time to *do* something. A **Skill** is a declarative YAML/JSON *recipe* (Contract 11) — the runtime's `SKILL_LOAD` stage never invokes it directly; it just splices the skill's name/description into the LLM's prompt. A skill's `steps[]` reference tools (`tool-web-search.web_search`), LLM calls, or other services (`memory.retrieve`) using the *same* JSON-Schema dialect as tool manifests specifically so a tool's output can flow into a skill's input without translation.

Both registries (`tool-registry`, `skill-registry`) share an identical design:

- **Registration** — an admin call with a Contract-4 manifest; a 4th version retires the oldest of 3 active ones
- **Discovery** — `GET /v1/tools` returns the union of platform rows and your tenant's own, with tenant rows shadowing a platform row of the same name
- **Health polling** — every 30s, `GET {base_url}/manifest`; 1 failure → `degraded`, 3 consecutive → `offline`, a single success snaps straight back to `active`. Health is a **signal surfaced to callers**, not an enforcement gate inside the registry itself
- **RLS** — a three-policy split (`read`/`write`/`platform`) specifically closes a "marketplace hole" where a naive policy would let a tenant forge a platform-visible row by writing `tenant_id = NULL`
- **Per-agent access control** — independent of tenant ownership, `agent_tool_access`/`agent_skill_access` gate individual agents to `none`/`ask`/`automated` per tool or skill

**`tool-web-search` end to end:** `POST /mcp/v1/invoke {"args": {"query": "...", "max_results": 5}}` → dual-mode JWT auth → coarse + fine scope check (`tool:invoke` + `tool:tool-web-search:invoke`) → idempotency replay check → rate limit (60/min/tenant, Valkey, fails open) → JSON-Schema arg validation → provider call (`mock` by default; `serpapi`/`brave` behind an API key) → a 10 MiB output cap that discards the whole oversized response rather than truncating it → cached under the idempotency key → `200`.

### 8.8 Frontend + BFF internals

Covered narratively in §4; the mechanical summary: the Next.js app never calls anything but the BFF (`credentials: 'include'` on every fetch); the BFF is the *only* thing holding a real downstream token; every proxied call gets its identity headers stripped from the client and re-injected server-side so the browser can never spoof `Authorization`/`X-Tenant-ID`; and the one deliberate exception to "buffered proxy" is the SSE relay for live task streaming.

---

## 9. Platform Contracts

Every service builds against a shared `contracts/` repo — 21 versioned, "written-once" agreements. This section is the reference card.

### 9.1 Identity — the JWT (Contract 1)

RS256 only (HS256 forbidden). Required claims: `iss, sub, aud, iat, exp, jti, tenant_id, agent_id`. Reserved-but-not-yet-enforced claims (must be *accepted*, not rejected, by every verifier): `cnf` (token binding), `wkl_id` (SPIFFE identity), `delegation_*` (A2A chains), `approval_context` (step-up grants), `behavior_policy_id`. Token lifetime for agent tokens ≤ 1 hour. JWKS cached ≤24h, refreshed on `kid` miss (rate-limited to 1/min); keys rotate every 90 days.

### 9.2 Errors (Contract 2)

Every error, everywhere: `{ "error": { "code", "message", "details", "request_id", "trace_id", "timestamp" } }`. `code` isn't a closed enum, but there's a large reserved list spanning the whole platform's failure modes — reading it is almost a table of contents:

```
UNAUTHORIZED · FORBIDDEN · NOT_FOUND · CONFLICT · VALIDATION_ERROR ·
RATE_LIMIT_EXCEEDED · INTERNAL_ERROR · SERVICE_UNAVAILABLE ·
GUARDRAIL_VIOLATION · BUDGET_EXCEEDED · QUOTA_EXCEEDED · TENANT_SUSPENDED ·
IDEMPOTENCY_KEY_CONFLICT · IDEMPOTENCY_REQUEST_IN_FLIGHT ·
APPROVAL_REQUIRED · APPROVAL_EXHAUSTED · DELEGATION_CHAIN_INVALID ·
DELEGATION_CYCLE · TOKEN_REPLAYED · TOKEN_REVOKED · STEP_UP_REQUIRED ·
SIGNUP_DISPOSABLE_EMAIL · SIGNUP_VERIFICATION_EXPIRED · SIGNUP_RATE_LIMITED ·
TENANT_PENDING_DELETION · WEBHOOK_SIGNATURE_INVALID · WEBHOOK_REPLAY_REJECTED
```

### 9.3 Tenant model & isolation (Contract 13)

`tenant_id` is a UUID **owned by Auth**, resolved only from the verified JWT — never a request body. Enforced architecturally, not just by policy: every tenant-scoped table has Postgres Row-Level Security keyed on a transaction-local `SET LOCAL app.tenant_id`; the connection pooler must run in **transaction mode** (session mode leaks the setting across requests). Every new tenant-scoped table requires a CI test proving a cross-tenant read returns zero rows before it can merge. Two reserved UUIDs are constant across the whole platform: `...0001` = the platform tenant, `...00ff` = the CI-only integration-test tenant.

### 9.4 Tracing & headers (Contract 8)

W3C `traceparent`/`tracestate` on every call, forwarded unchanged (only the span id updates) through every downstream hop. `X-Request-ID`/`X-Tenant-ID`/`X-Agent-ID` are edge-injected correlation headers — **correlation only, never identity**; every service still derives the authoritative tenant/agent from the verified JWT itself. In the current compose runtime (no Kong yet), the frontend BFF injects `X-Tenant-ID` from its own session instead.

### 9.5 Idempotency & versioning (Contract 9)

Routes are prefixed `/v1/`, `/v2/`, with a minimum 90-day sunset notice on removal. Idempotency is a fully worked-out state machine: same key + same body fingerprint → replay the cached response; same key + different fingerprint → `409`; concurrent duplicate → `409 IN_FLIGHT`; **idempotency-store outage fails closed** (`503`) — "duplicate side effects are worse than a transient error." Pagination is cursor-only, never offset-based.

### 9.6 Onboarding (Contract 20)

See the worked example in [§5.1](#51-becoming-a-tenant-external-onboarding-contract-20).

### 9.7 Approval / step-up (Contract 16)

A designed-now, enforced-later (Phase 2) human-in-the-loop gate: an `X-Approval-Token` alongside the normal agent JWT for scopes like `payments:execute`, `data:bulk_delete`, `infra:write`, `agent:create_subagent`. The approver must be a different human than the one who owns the agent's credentials — "prevents self-approval on hijacked credentials." One-shot tokens live ≤15 minutes; multi-shot grants ≤1 hour and require an explicit business reason.

### 9.8 Billing & usage metering (Contract 19)

Every SharedCore service emits one usage event per billable operation onto its own `cypherx.<service>.usage.recorded` topic — "**never sampled** — loss is revenue loss," always via the transactional outbox pattern. A quota model lives in a single JSONB column on the tenant record, with per-service blocks (`llms.requests_per_min`, `rag.storage_bytes_max`, `xagent.concurrent_tasks_max`, etc.) that fall back to plan defaults when unset. Unit costs are owned by whichever service performed the work (e.g., guardrails prices its own rules from $0.000001/call for cheap regex up to $0.005/call for a heavyweight classifier) — usage events carry only units + correlation ids, never a hardcoded price.

### 9.9 A2A task delegation (Contract 3)

The task-type registry is closed and PR-gated: `research`, `summarise`, `code-review`, `generate`, `classify`, `extract`, `plan`, `chat`. A completed task response always carries `cost_usd` and an ordered `task_steps[]` trace. The **delegation chain** (who can act on whose behalf) travels only in JWT claims, never the message body, and must be monotonically non-increasing in scope at every hop — a delegate can never grant itself more authority than it was given. This whole mechanism is designed but enforced starting Phase 10 (ax-2).

### 9.10 Kafka event envelope (Contract 5)

Every event, regardless of producer: `{event_id, event_type, schema_version, produced_at, tenant_id, producer_service, partition_key, payload}`. Topic names follow `cypherx.<domain>.<entity>.<event-type>`; `partition_key` defaults to `tenant_id` for per-tenant ordering (two compact agent-lifecycle topics deliberately key on `agent_id` instead). Every non-compact topic has a paired `.dlq`. The only allowed foreign (non-`cypherx.`) prefix is `px0.*`, consumed exclusively by a `px0-bridge` adapter that translates it into native `cypherx.tenant.*` events — no other service ever subscribes to `px0.*` directly.

### 9.11 Health & observability (Contract 6, 7)

Every service: `/livez` (process-only — must never touch a DB/Kafka/downstream, so a blip never gets a healthy pod killed), `/readyz` (does check downstreams, pulls the pod from the load balancer on failure), `/metrics` (Prometheus, cluster-internal only). Logs are structured JSON with `trace_id`/`tenant_id`/`agent_id` on every line.

---

## 10. The Canonical End-to-End Test (Contract 15)

`contracts/smoke-tests/first-cycle.md` is, in its own words, *"the unambiguous definition of done"* — the platform is only considered working when this exact 15-case scenario passes twice in a row against a freshly, cold-deployed environment. Cases 1–10 gate the core spine; 11–15 gate the "enterprise wave" (onboarding, idempotency, rate limiting, OIDC discovery).

| # | Scenario | Proves |
|---|---|---|
| 1 | Submit "What is 2+2?" | The whole spine works: `200`, answer contains "4", `tokens_used`/`cost_usd` > 0 |
| 2 | Submit a prompt-injection attempt | `422 GUARDRAIL_VIOLATION` — blocked before the LLM ever runs |
| 3 | Submit a message containing an email address | `200`, but the email is redacted before it reaches the model |
| 4 | Tenant B's JWT tries to read tenant A's task | **`404`, not `403`** — "leaking existence is itself a tenant-isolation bug" |
| 5 | Call with no `Authorization` header | `401` at the edge |
| 6 | Run 5 tasks, then read the Kafka topic from scratch | Exactly 5 well-formed usage events, matching `trace_id`s |
| 7 | Fetch a completed task | `task_steps` shows `[guardrail_check_input, llm_call, guardrail_check_output]` in order |
| 8 | Search the trace collector by `trace_id` | One trace spans xAgent → Guardrails → LLMs → provider |
| 9 | Pull recent logs for `service="xagent"` | 100% valid structured JSON, zero parse errors |
| 10 | Hit `/livez` + `/readyz` on Auth/LLMs/Guardrails/xAgent | All `200`, correct shape |
| 11 | Full self-serve onboarding → sandbox key → a real chat call | All steps 2xx; tenant's `source='self-serve-signup'` |
| 12 | Replay the same `Idempotency-Key` + body twice | Second call replays the cached response, **no second LLM call fires** |
| 13 | Same key, different body | `409 IDEMPOTENCY_KEY_CONFLICT` |
| 14 | Exceed the free-tier rate limit | `429` with correct `Retry-After`/`X-RateLimit-*` headers |
| 15 | Fetch OIDC discovery document | Valid, spec-shaped JSON |

Read end to end, cases 1–10 **are** the platform's canonical single-turn story, and cases 11–15 layer the business edges (becoming a tenant, safe retries, abuse protection, and the one piece of infrastructure trust every SDK depends on) on top of it.

---

## 11. Deployment & Environments

- **Local dev:** `infra/compose/docker-compose.yml` runs the entire platform against an external Neon Postgres, with Redpanda/Valkey/MinIO as local containers and a single Caddy edge on `:8000`. Everything defaults to keyless/mocked (`MOCK_PROVIDERS=true`, `SEARCH_PROVIDER=mock`, etc.) so the whole stack runs with zero API keys.
- **Cloud target:** Terraform + Terragrunt provision AWS (EKS, RDS/MSK/ElastiCache-equivalents, Route53/ACM) per environment (`dev`/`staging`/`prod`), with a strict IAM role split (a GitHub Actions OIDC role that is explicitly denied any IAM action, separate Terraform-infra vs. Terraform-IAM roles). **No AWS resources have actually been applied yet** — this is committed IaC only.
- **Kubernetes packaging:** every service consumes one shared Helm chart, `charts/cypherx-service`, so platform-wide contracts (health probes, structured logs, trace propagation, tenant/RLS DSN shape, migration-as-a-job) can't drift per service.
- **GitOps:** `gitops/` is an ArgoCD App-of-Apps repo — dev/staging auto-sync on merge, **prod requires a human to click "sync"** (no `automated:` block on purpose — this is the prod safety gate). As of this snapshot the repo has the three root Applications but **zero actual service child-apps** committed yet.

---

## 12. What's Real vs. What's Planned

The planning docs under `archive/Manoj/phases/` mark almost every phase as "⏳ pending," which **understates** what's actually in this repository — the code and ~450+ test files across services tell a more complete story. Being precise about the gap:

**Solidly implemented and tested today:** Auth, Guardrails, LLMs Gateway, Memory, RAG, xAgent/ax-1 (including RAG/Memory/Tool-loop integration stages), Tool Registry, Skill Registry, tool-web-search, the Console frontend + BFF, cypherx-a1 + mcp-eng-memory, the `contracts/` repo itself, the base Helm chart, and the Terraform module library (as code, not as applied infrastructure).

**Explicitly stubs / not built:**
- **`platform/`** (the control plane / billing-rollup / config-management service) — literally just an unedited GitLab README. Nothing to deploy.
- **`xAgent/ax-2`** (A2A router + multi-agent Orchestrator) — a `CLAUDE.md` describing the design and nothing else. Real agent-to-agent delegation does not exist yet; the Console's "Orchestrator" screen manages scope-inheritance for sub-agents today, which is not the same thing as ax-2's planned DAG workflow engine.
- **`gitops/`** service child-apps — the App-of-Apps roots exist; no actual service is deployed through them in this repo.
- The email-verification onboarding **UI** (Contract 20's first half) — the backend routes and the verification landing page both work, but no page in the shipped app actually starts that funnel; only the instant self-serve `/register` path is reachable by a real user today.

**Notable documentation drift found during this research** (worth fixing, listed here so they aren't silently repeated):
- `frontend/CLAUDE.md` describes a tenant/agent/API-key "platform-credential" login that the real code has replaced with email/password + Google OAuth + self-serve register.
- `cypherx-a1/openapi.yaml` doesn't list `/v1/graph/activity`, even though it's live and used by the `mcp-eng-memory` manifest.
- `Skills/skill-registry/db/migrations/README.md` still describes 2 old migration files; 4 newer ones exist on disk.
- `tool-web-search`'s own `REPO_ANALYSIS_2026-06-11.md` predates the shipped wire format — the real request body key is `args`, not the spec's original `input`.

---

## 13. Source Map

This document was built from direct reads of, in addition to the code/tests cited inline above:

- `archive/Manoj/CYPHERX_AI_PLATFORM_PLAN.md`, `archive/Manoj/phases/README.md`, `archive/Manoj/stack.md`, `archive/Manoj/phases/phase-00-contracts.md`
- `infra/CLAUDE.md`, `gitops/CLAUDE.md`, `charts/CLAUDE.md`, `archive/CLAUDE.md`, `platform/CLAUDE.md`
- Every service's own `CLAUDE.md`/`README.md` under `Shared Core/`, `xAgent/`, `Tools/`, `Skills/`, `CoreProjects/cypherx-a1/`, `frontend/`
- `contracts/` in full (all 21 numbered contracts plus the smoke-test, onboarding, billing, and webhook specs)
- Representative test files, database migrations, and OpenAPI specs per service, opened directly where the docs were ambiguous or stale

Where this document states something as fact about *current* behavior, it's grounded in source/tests/migrations, not just the planning docs — the planning docs (`archive/Manoj/`) are cited only for vision, vocabulary, and the original design rationale.
