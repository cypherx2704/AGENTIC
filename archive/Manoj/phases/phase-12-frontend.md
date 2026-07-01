# Phase 12 — Frontend
> **Status:** ⏳ Pending | **Depends On:** Phase 2–9 + named endpoint work packages WP04 (Auth), WP05 (LLMs), WP07 (Guardrails), WP08 (xAgent) — see "Named Cross-Phase Endpoint Dependencies" below | **Blocks:** Phase 13
> **First Cycle:** 📋 Not gating the platform's first cycle. Begin in parallel with Phase 11 — BUT the FIRST Phase-12 release targets the first-cycle runtime and services ONLY (compose + Neon + Valkey + Redpanda + MinIO; Auth / LLMs / Guardrails / xAgent screens — no px0, no Kong, no AWS/K8s). See the ⚡ First-Release Checklist.

## Amendment Log (2026-06 — pre-build reconciliation)

- **Platform-credential login replaces px0-SSO-only auth (BLOCKER fix).** Component 1 is now a pluggable BFF auth provider. First cycle = `platform-credential`: operator submits tenant_id + admin agent API key; BFF validates via the already-built `POST /v1/agents/{id}/token` exchange, stores credentials server-side in the Valkey session, and re-mints short-lived JWTs on demand. px0 OIDC slots in later behind the SAME `/bff/me` contract and identical session/CSRF shell. The nonexistent `agent:mint_on_behalf` mint mode and the `platform-migrations/phase-12/` package are deferred to Phase 11.
- **BFF trust-boundary duties made explicit (was Kong's job; no Kong exists first cycle).** The BFF MUST strip all client-supplied identity headers (`Authorization`, `X-Tenant-ID`, `X-Forwarded-Agent-JWT`, `X-Request-ID`) on every inbound browser request, inject `X-Tenant-ID` from the server-side session, and mint a fresh `X-Request-ID` per proxied call (propagating `traceparent`).
- **Six previously-assumed endpoints declared as named WP dependencies.** They exist nowhere yet: Auth `GET /v1/agents` (list) + `PATCH /v1/agents/{id}` (WP04); LLMs `GET /v1/usage` + `GET /v1/models` (WP05); Guardrails `GET /v1/violations` (WP07); xAgent `GET /v1/tasks` (list) + `GET/PUT /v1/agents/{id}/runtime` (WP08). Frontend screens consuming them may not be built ahead of the owning package.
- **Hosting/CI re-targeted to the actual runtime.** First cycle: SPA static export served by the BFF container under compose, all service URLs env-driven, CI on GitLab CI (the polyrepo host — there are no GitHub Actions). S3 + CloudFront + Kong + Terraform + ArgoCD are retained ONLY as the documented cloud (full-enterprise deploy-target) form; checklist split into service-code vs deploy-target accordingly.
- **Screens rescoped to first-cycle services.** First release ships Agent Builder/List/Test, Task Monitor, the Auth / LLMs / Guardrails dashboards, and agent API-key management. Memory Dashboard (Phase 6), RAG Dashboard (Phase 5), Skill Library/Editor (Phase 8), Tool Registry browser (Phase 7), and team management + billing iframe (px0 — Phase 11) move to their owning phases.
- **Editorial corrections (same audit).** Model dropdown rendered from live `GET /v1/models`, never hardcoded; `memory_scope` enum rendered from the live runtime API/code (including `session`); audit-log path corrected to `GET /v1/audit-log`; test-runner cost banner is a client-side worst-case computed from `token_budget_per_task`, with actual cost shown post-run.
- **Memory Dashboard user-scope rule corrected to the decided Phase-6 default.** The earlier "user-scope: tenant-shared by design" line encoded the pre-Round-2 leak: user scope is **principal_only visibility by default** (JWT-resolved principal must match), governed by `memory.tenant_config.user_scope_visibility`; tenant-shared applies ONLY when the tenant_config opts in. Dashboard text aligned to Phase 6 Components 2/2b.

## Named Cross-Phase Endpoint Dependencies (NEW — see Amendment Log)

| Endpoint | Consumed by | Owning work package |
|---|---|---|
| Auth `GET /v1/agents` (tenant-scoped list) | Agent List, Component 2 | **WP04** |
| Auth `PATCH /v1/agents/{id}` | Agent edit, Component 2 | **WP04** |
| LLMs `GET /v1/usage` | LLMs Dashboard, Component 5 | **WP05** |
| LLMs `GET /v1/models` | Model dropdown (Component 2), dashboard labels | **WP05** |
| Guardrails `GET /v1/violations` | Guardrails Dashboard violation log, Component 5 | **WP07** |
| xAgent `GET /v1/tasks` (list) + `GET/PUT /v1/agents/{id}/runtime` | Task Feed (Component 3), two-step publish (Component 2) | **WP08** |

---

## Phase Overview

The Frontend is **all UI for the CypherX AI platform** (excluding px0's own UI). It provides the agent builder, orchestration canvas, SharedCore dashboards, and developer tooling. Auth is handled by a pluggable BFF auth provider: first cycle uses platform-credential login (tenant_id + admin agent API key, exchanged server-side for short-lived JWTs); px0 SSO is the later cloud provider behind the same `/bff/me` contract. The frontend consumes CypherX AI platform APIs.

> **External-operability stance — three deployment modes (NEW):**
>
> Every SharedCore service is "externally operable" (Master Plan principle). The UI tier must honour the same:
>
> 1. **`bundled`** (default — this Phase 12) — the monolithic SPA at `app.cypherx.ai`. Single UI for the whole platform: agent builder + orchestration + all 7 SharedCore dashboards + marketplace.
> 2. **`per-service mini-UIs`** (📋 — Phase 13+) — one minimal admin SPA per SharedCore service (e.g., `https://memory.cypherx.ai/admin/`) that an external customer running ONLY that service can host. Implemented as a per-service Next.js app, shipped from the same monorepo, building from a shared component library (`@cypherx/admin-ui`). Each per-service UI talks to the BFF for THAT service only.
> 3. **`headless / API-only`** — no UI shipped at all. External customers integrate via the Admin REST APIs (every SharedCore service exposes its admin endpoints publicly — via Kong in the cloud form, directly published in compose/self-host; see each service's Phase doc). Default for self-hosted enterprise customers who supply their own admin tooling.
>
> Mode is chosen at deployment time, not by code paths in the SPA. The monolithic SPA can be disabled per-tenant via `auth.tenants.source_metadata.frontend_mode = bundled | mini | headless`.

**Deliverable:** A web application providing agent management, task monitoring, and SharedCore service dashboards.

> 🏗️ **Service Architecture Note:** The frontend framework, component library, state management approach, and routing architecture must be planned separately before implementation begins. Technology choices (Next.js, React, Remix, etc.) must be confirmed before design begins.

---

## High Level Design

### Application Structure (BFF-fronted SPA)

```
Browser (SPA — env-driven origin; first cycle: the BFF's own compose-published host;
         cloud form: https://app.cypherx.ai)
   │  httpOnly session cookie + double-submit CSRF token
   ▼
Frontend BFF (first cycle: compose service `frontend-bff`; cloud form: K8s ns frontend-bff)
   │  holds session credentials server-side; re-mints short-lived agent JWTs on demand;
   │  TRUST BOUNDARY: strips client-supplied identity headers, injects X-Tenant-ID from
   │  the session + a fresh X-Request-ID per proxied call
   │  (px0 mode, 📋 Phase 11: service-JWT + X-Forwarded-Agent-JWT per Contract 12)
   ▼
Auth / LLMs / Guardrails / xAgent   (first-cycle services; RAG / Memory / Tools /
                                     Registry / Platform-Mgmt join as Phases 5–8/11 land)
```

```
Frontend Application (SPA)
│
├── Authentication Layer        (pluggable provider: platform-credential ⚡ first cycle |
│                                 px0 SSO 📋 Phase 11 → BFF session cookie → SPA never
│                                 sees JWT directly)
│
├── Agent Management
│   ├── Agent Builder           (create/edit agent definitions — two-step publish)
│   ├── Agent List              (browse, search, filter agents)
│   └── Agent Testing           (real task submission via BFF-minted agent JWT;
│                                 labelled "sandbox" but billed normally)
│
├── Task & Workflow Monitoring
│   ├── Task Feed               (first-cycle: long-poll; SSE multiplexed feed is 📋 + gated on xAgent)
│   ├── Task Detail             (execution timeline, step-by-step view)
│   └── Workflow Canvas         (visual DAG — gated on Phase 10; NOT in first Phase 12 release)
│
├── SharedCore Dashboards
│   ├── Auth Dashboard          (agents, agent API keys, paginated audit log)
│   ├── LLMs Dashboard          (usage, cost, latency, quota)
│   ├── Guardrails Dashboard    (violation log, policy editor as version-publisher)
│   ├── Memory Dashboard        (admin-only memory explorer — 📋 gated on Phase 6)
│   └── RAG Dashboard           (knowledge bases, ingestion status, test query —
│                                📋 gated on Phase 5)
│
├── Skills & Tools              (📋 — gated on Phases 7/8; not in first release)
│   ├── Skill Library           (browse, search, preview skills)
│   ├── Skill Editor            (YAML editor; Contract 11 JSON Schema validation)
│   ├── Tool Registry           (browse tools per version + deprecation banners)
│   └── Tool Test Console       (invoke tools from UI via BFF)
│
└── Settings & Admin
    ├── API Key Management      (AGENT API keys — Phase 2 auth.api_keys; not user keys)
    ├── Team Management         (invite members, assign roles — via px0; 📋 Phase 11)
    └── Billing Overview        (embedded iframe from px0 billing, CSP-whitelisted;
                                 📋 Phase 11)
```

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> Items marked ⚡ form the first Phase-12 release and must be buildable in the first-cycle
> runtime (compose + Neon + Valkey + Redpanda + MinIO — no AWS/K8s/Kong/px0). 📋 items
> follow their gating phases (see Amendment Log).

---

### Component 1 — Authentication Integration ⚡/📋 (BFF pattern, pluggable provider)

> **The prior draft was self-contradicting** — it claimed both `httpOnly cookie (not localStorage)` AND `Authorization: Bearer <user-jwt>` in fetch headers. `httpOnly` cookies are invisible to JavaScript by design, so the SPA literally CANNOT put their value in a header. Either you ship an XSS hole (localStorage + header) or commit to a server-side session. We commit to server-side session via a Backend-for-Frontend.

**Pluggable auth provider (`BFF_AUTH_PROVIDER` env — AMENDED, see Amendment Log):**
The session shell (httpOnly session cookie + double-submit CSRF + Valkey session store + `GET /bff/me`) is provider-agnostic. Two providers share it; the SPA never branches on provider.

**`platform-credential` provider (⚡ first cycle — the only provider buildable in the actual runtime):**
```
  1. SPA shows a login form: tenant_id + agent_id + admin agent API key.
  2. POST /bff/login {tenant_id, agent_id, api_key} → BFF validates by performing the
     already-built Auth exchange POST /v1/agents/{agent_id}/token with the submitted key.
     Failure → 401 to the SPA; nothing stored.
  3. On success BFF stores {tenant_id, agent_id, api_key} SERVER-SIDE in the Valkey
     session (never returned to the browser) and sets the same two cookies as the OIDC
     flow below (session=<opaque-id> HttpOnly + csrf=<random> double-submit).
  4. On every proxied downstream call the BFF re-mints a short-lived agent JWT from the
     stored key on demand (cached in the session until ~30s before exp).
  5. GET /bff/me returns { provider: "platform-credential", tenant_id,
     principal: <agent_id>, scopes } — the SAME response contract px0 mode serves later.
  Logout: POST /bff/logout deletes the Valkey session (stored credentials included) and
  clears both cookies.
  CSRF protection: identical to the px0 flow below — double-submit on every mutation.
```

**`px0-oidc` provider flow (📋 — Phase 11+, once px0 exists; slots in behind the same `/bff/me` contract):**
```
  1. User visits https://app.cypherx.ai → SPA loads → SPA calls GET /bff/me.
  2. /bff/me returns 401 → SPA redirects to /bff/login → BFF redirects to px0 SSO.
  3. px0 SSO returns user JWT to BFF callback (/bff/auth/callback).
  4. BFF:
       - Verifies user JWT against px0 JWKS.
       - Stores user JWT in server-side session store (Valkey, TTL = JWT exp).
       - Returns Set-Cookie:
           session=<opaque-id>; HttpOnly; Secure; SameSite=Strict; Path=/
       - Returns Set-Cookie:
           csrf=<random-token>; Secure; SameSite=Strict; Path=/
         (NOT HttpOnly — SPA reads + double-submits on mutations.)
  5. SPA fetches /bff/* with credentials: 'include'. BFF reads session cookie,
     loads user JWT, proxies to backend.
  6. SPA NEVER sees the user JWT. SPA NEVER calls api.cypherx.ai directly.

CSRF protection:
  - Every state-changing /bff/* request (POST/PUT/PATCH/DELETE) MUST carry
    X-CSRF-Token header matching the csrf cookie value (double-submit pattern).
  - Mismatch → 403; BFF logs + Prometheus counter csrf_violations_total.

Session refresh:
  Background BFF process refreshes the user JWT in Valkey before expiry using
  the px0 refresh token (also held server-side). SPA is unaware — the session
  cookie remains valid until idle timeout (24h).

Logout:
  POST /bff/logout → BFF deletes Valkey session, clears both cookies,
  calls px0 logout endpoint.
```

> **Backend service auth + trust boundary (downstream of BFF — AMENDED):**
> The BFF is the trust boundary that the cloud form delegates to Kong; no Kong exists in the first-cycle runtime. On EVERY inbound browser request the BFF MUST strip any client-supplied `Authorization`, `X-Tenant-ID`, `X-Forwarded-Agent-JWT`, and `X-Request-ID` headers before proxying. When proxying to a backend service, BFF sends:
>   `Authorization: Bearer <re-minted session agent-JWT>`   (⚡ platform-credential mode)
>   `X-Tenant-ID: <tenant_id from the server-side session>`  (BFF-injected; never client-supplied)
>   `X-Request-ID: <BFF-minted UUIDv4 per proxied call>`
>   `traceparent: <propagated>`
> px0 mode (📋 Phase 11) keeps the same header set but authenticates as the BFF's own service identity: `Authorization: Bearer <frontend-bff service-JWT>` (Contract 12, via `service-auth/frontend-bff/bootstrap_secret`) + `X-Forwarded-Agent-JWT: <user-JWT or BFF-minted agent-JWT>` — same pattern as xAgent → Guardrails, etc.

> **What the user JWT can and cannot do (📋 — px0 mode only):**
> The user JWT (from px0) carries `user_id`, `org_id`, and px0-issued scopes (e.g., `cypherx:admin`). It is sufficient for **admin/management** endpoints (agent CRUD, dashboard reads, key issuance). It is **NOT sufficient for agent-execution** endpoints (`POST /v1/tasks`, etc.) which require an agent JWT per Contract 1. The mint path is in Component 2.
> In ⚡ platform-credential mode there is no user JWT: the session's admin agent JWT (carrying `agent:admin`) covers admin/management endpoints, and agent execution uses the API-key → JWT exchange in Component 2.

---

### Component 2 — Agent Builder ⚡ (first-cycle services only; mint-on-behalf path 📋 → Phase 11)

**Visual form-based agent configuration:**
```
Fields:
  Name, Description, Version
  System Prompt           (rich text editor with variable hints)
  LLM Model               (dropdown rendered from live LLMs GET /v1/models — WP05 named
                           dependency; aliases + capabilities come from the API, never
                           hardcoded in the SPA)
  Temperature, Max Tokens (sliders; CHECK temperature ∈ [0.0, 2.0] per Phase 9)
  Memory Scope            (radio — options rendered from the live runtime API/code enum,
                           INCLUDING 'session'; never hardcoded in the SPA)
                          (NOT 'global' — Phase 6/9 post-edit renamed; sending 'global'
                           returns VALIDATION_ERROR)
  Allowed Tools           (📋 — gated on Phase 7; hidden in first release;
                           multi-select with per-tool VERSION dropdown;
                           default '@latest'; deprecation banner with sunset date;
                           wire format: ["tool-web-search@1.2.0", "tool-X@latest"]
                           per Phase 7 post-edit)
  Allowed Skills          (📋 — gated on Phase 8; hidden in first release;
                           multi-select from skill library; version-pinned similarly)
  Guardrail Policy        (dropdown: platform default | tenant default | custom)
  Token Budget Per Task   (number input)

Two-step publish (Phase 9 post-edit lifecycle):
  Step 1: POST /v1/agents  on auth-service       (creates the identity in auth.agents)
  Step 2: PUT /v1/agents/{agent_id}/runtime  on xagent-service
                                                  (creates/updates xagent.agents runtime
                                                   config; GET/PUT pair — WP08 named
                                                   dependency, see Amendment Log)
  UI shows progress for BOTH; on Step 2 failure surfaces "Agent created but runtime
  not configured — retry" and offers a single-button retry of Step 2 (idempotent).
  Do NOT roll back the Auth row on Step 2 failure; identity is safe to keep.

  Agent List / edit screens consume Auth GET /v1/agents (tenant-scoped list) +
  PATCH /v1/agents/{id} — WP04 named dependency; neither endpoint exists yet and the
  screens may not be built ahead of WP04.

Actions:
  Save as Draft (BFF stores draft in browser localStorage or BFF session — not persisted to Auth)
  Publish        (two-step flow above)
  Test           (real task submission — see "Sandbox test runner" below)
  Version History (list of past xagent.agents versions; diff view is 📋)

Sandbox test runner (Action: Test):
  xAgent has NO sandbox mode. "Test" submits a real POST /v1/tasks; real LLM
  tokens are consumed; real cost is billed to the tenant.

  UI MUST:
    - Show a worst-case cost banner BEFORE running, computed CLIENT-SIDE from the agent's
      token_budget_per_task × model pricing (pricing from GET /v1/models — WP05); no
      estimate endpoint exists. Show the ACTUAL cost (from the completed task record)
      post-run.
    - Set body.metadata.test = true on the submitted task (Phase 3 reserved-key list
      excludes 'test' so this is allowed in metadata).
    - Filter sandbox tasks out of the default LLMs dashboard view via metadata.test.

  BFF flow for Test (⚡ first cycle — API-key → JWT exchange; per the Phase-9 amendment
  body.agent_id MUST equal the JWT's agent_id, else xAgent returns 422 VALIDATION_ERROR):
    1. SPA POSTs /bff/agents/{agent_id}/test  { input: {...} }  with session cookie + CSRF.
    2. Testing agent X requires agent X's API key (issued via the Component 6 UI). The
       operator pastes it ONCE into the test modal; BFF stores it ONLY in the Valkey
       session (never persisted client-side, never echoed back). If the agent under test
       IS the session's admin agent, the stored session credential is reused directly.
    3. BFF exchanges the key via Auth POST /v1/agents/{agent_id}/token — the same
       exchange as login (Component 1). Auth returns a 5-minute agent JWT (Contract 1 TTL).
    4. BFF POSTs to xagent /v1/tasks with:
         Authorization: Bearer <minted agent JWT>
         X-Tenant-ID / X-Request-ID / traceparent per the Component 1 trust-boundary
         contract; body.agent_id = the JWT's agent_id.
    5. BFF returns task_id to SPA; SPA either long-polls
       /bff/tasks/{task_id} or subscribes to /bff/tasks/{task_id}/stream (📋).

  Why this model:
    - User-style credentials alone don't satisfy Contract 1 (no agent_id, no agent
      scope shape) — execution always needs an agent JWT.
    - Letting the SPA hold an agent JWT or API key would put a long-lived secret in
      browser memory; both live only in the server-side Valkey session.
    - No long-lived agent credentials are stored by the platform on the operator's
      behalf (no Secrets Manager object per agent, no rotation runbook per agent).

  (moved to Phase 11 — see Amendment Log) The `agent:mint_on_behalf` delegation path —
  user-JWT-authorised minting of ANY agent's JWT with `on_behalf_of` audit attribution
  and no per-agent key paste — replaces step 2 once px0 SSO lands. Its scope
  registration, deny-by-default platform-policy entry, and `auth.service_acl` grant to
  `frontend-bff` ship with Phase 11's platform-migrations. The same path is then reused
  by the RAG Dashboard test query and Memory Dashboard admin reads (both themselves
  gated on Phases 5/6); there is no second BFF→agent path in the platform.
```

---

### Component 3 — Task Monitor (Real-time) ⚡ (long-poll; list endpoint = WP08 named dependency)

```
Task Feed (first cycle: long-poll):
  SPA polls GET /bff/tasks?since=<RFC3339>&limit=50  every 5s.
  BFF proxies to xagent GET /v1/tasks?since=...&limit=... (tenant_id derived from the
  server-side session in BFF and sent as X-Tenant-ID per Component 1 — NOT a query
  param; passing tenant_id in URL would violate Contract 13).
  NAMED DEPENDENCY: the xAgent task-list endpoint does not exist yet — scheduled in
  WP08 (see Amendment Log); the Task Feed may not be built ahead of it.
  Returns paginated rows: task_id, agent, status, submitted_at, tokens_used, cost.

  Note: `live=true` and tenant-wide SSE feed were in the prior draft. Both removed:
    - `live=true` is non-standard; conflates list with stream.
    - Per-task SSE (one connection per visible task) hits the browser's 6-per-origin
      concurrency cap before a dashboard with 10 tasks loads. Page dies silently.

Task Feed (📋 — when xAgent multiplexed feed lands):
  SSE connection to GET /bff/tasks/feed  (single connection, multiplexes all tenant tasks).
  Requires xAgent to expose POST /v1/tasks/feed which subscribes to a per-tenant Valkey
  pub/sub channel — listed as 📋 in Phase 9 post-edit.

Task Detail View (uses GET /bff/tasks/{task_id}; SSE per single task is OK at this scale):
  Execution Timeline (step-by-step, with timing — sourced from xagent.task_steps):
    ├── ✅ Input Guardrail Check   (18ms)   [step_name=guardrail_check_input]
    ├── ✅ Memory Retrieved        (32ms)   [📋 — only present when MEMORY_RETRIEVE enabled]
    ├── 🔄 LLM Call                (in progress...)  [step_name=llm_call]
    │     └── 🔄 Tool: web_search  (234ms)  [📋 — only when TOOL_LOOP enabled]
    └── ⏳ Output Guardrail Check  (pending) [step_name=guardrail_check_output]

  Step name discriminator matches Phase 9 task_steps.step_name column post-edit
  (`guardrail_check_input`, `llm_call`, `tool_call:<server>.<fn>`, `guardrail_check_output`,
  `memory_retrieve`, `memory_write`, `skill_load`).

  Token usage breakdown
  Cost breakdown (including cached_prompt_tokens + cache_creation_tokens from Phase 3)
  Full input/output (with expand/collapse; size-aware lazy load if > 100 KiB)
  Raw trace link (opens Grafana Tempo via trace_id; uses tracestate per Contract 8)
```

---

### Component 4 — Orchestration Canvas 📋 (gated on Phase 10)

> **NOT in first Phase 12 release.** This UI consumes `POST /v1/workflows`, `xagent.workflows`, and `xagent.workflow_tasks` — all of which are Phase 10 (`📋 Not required for first cycle`). Phase 12 ships without the canvas; canvas lands after Phase 10 stabilises. Mark this dependency loudly in the project plan so the UI team doesn't start before backend exists.

```
Visual DAG workflow builder:
  Node types:
    Agent Node     (a specialist agent — drag from agent library)
    Condition Node (if/else branching)
    Loop Node      (repeat N times or while condition)
    Approval Node  (human-in-the-loop checkpoint; respects 24h default approval window)
    Merge Node     (combine parallel outputs)

  Edge: data flow from node output to node input
  Variable binding: {{node.output.field}} syntax in node inputs
                    (must use the same Pongo2/Jinja2 helpers as Skills Phase 8 —
                     platform-provided list only; unknown helpers fail validation)

  Run Workflow: POST /bff/workflows from canvas (BFF proxies to xagent /v1/workflows)
  Live execution: colour-coded node status (pending/running/done/failed) via
                  the same long-poll-then-SSE pattern as Component 3.
```

---

### Component 5 — SharedCore Dashboards ⚡ Auth/LLMs/Guardrails | 📋 Memory (Phase 6), RAG (Phase 5)

**LLMs Dashboard (⚡):**
```
Charts:
  - Requests/minute (line chart, last 24h)
  - Token usage by provider/model (stacked bar, by day)
  - Cost by agent (table, sortable)
  - Latency p50/p95/p99 by provider (line chart)
  - Rate limit events (count, timeline)

Data sources:
  - Usage / token / cost tables: LLMs GET /v1/usage — WP05 named dependency
    (endpoint does not exist yet; dashboard may not be built ahead of WP05)
  - Model metadata for filters/labels: LLMs GET /v1/models (WP05)
  - Latency / rate charts: Prometheus + Grafana (embedded iframes or native API calls;
    optional in the compose runtime — panels hide gracefully when the observability
    stack is absent)
```

**Guardrails Dashboard (⚡):**
```
Charts:
  - Violations by rule (bar chart, last 7 days)
  - Block vs warn vs redact ratio (pie chart)
  - Violation log (table: agent, rule, text snippet [redaction-safe per Phase 4 post-edit], timestamp)
    Sourced from Guardrails GET /v1/violations — WP07 named dependency (endpoint does
    not exist yet; the log view may not be built ahead of WP07).

Policy editor (version-publisher, NOT in-place edit):
  Phase 4 post-edit made guardrails.policies append-only versioned. UI labels:
    Button: "Publish new version"  (NOT "Save")
    Sidebar: version history with diff + "Revert to this version" (creates new version
             copying the chosen prior content; old rows kept; status transitions
             active → superseded).
  Backed by POST /bff/policies (BFF proxies; no in-place mutation possible at the DB).
```

**Memory Dashboard (admin-only) (📋 — moved out of the first release; gated on Phase 6 Memory service; the cross-tenant mint pattern below additionally requires Phase 11's px0 user JWTs + platform-migrations — see Amendment Log):**
```
Memory explorer requires scope: platform:admin.
  (Earlier draft referenced `memory:admin` — that scope is not defined in Phase 2 or
  Phase 6. Use `platform:admin` only. Finer-grained service-specific admin scopes are
  📋 for the broader RBAC pass.)

  Phase 6 ownership rules (READ access):
    - tenant-scope memories: visible to platform:admin within tenant.
    - agent-scope: visible to the agent's owner OR platform:admin.
    - user-scope: **principal_only visibility by default** (per `memory.tenant_config.user_scope_visibility` — amended, see Amendment Log); tenant-shared ONLY if the tenant_config opts in. Visible to platform:admin within tenant in either mode.
    - session-scope: visible only to the agent that owns the session; admins see
      via platform:admin override.

  UI filters by scope/scope_id (agent dropdown, user_id input, session_id input).
  BFF enforces scope by re-checking the caller's scope on every memory list call —
  defence in depth on top of memory-service RLS.

Cross-tenant admin view (MANDATORY — Phase 6 RLS is strict per-tenant, the scope
alone doesn't unlock cross-tenant reads):

  A platform admin viewing tenant T's memories CANNOT just send their HOME-tenant
  JWT with `platform:admin` and expect cross-tenant data — Phase 6's RLS gates on
  `app.tenant_id = current_setting('app.tenant_id')`, which the JWT-derived tenant
  handler sets from the JWT's `tenant_id` claim. They'd see only their home tenant.

  Mechanism (canonical — mirrors Phase 8's cross-tenant Skills pattern):
    1. Platform pre-provisions a well-known agent under the platform tenant
       (Contract 13): `platform-admin-reader` agent_id `00000000-0000-0000-0000-0000000000a3`.
       This is a real auth.agents row with scope `platform:admin`. Seeded by
       Phase 11's platform-migrations (moved from Phase 12 — see Amendment Log).
    2. When a platform admin selects "View tenant T's memories" in the UI:
         a. BFF verifies the user holds `platform:admin` (from px0 user JWT).
         b. BFF mints a service JWT for `frontend-bff` with:
              tenant_id:    <target tenant T>     ← NOT the admin's home tenant
              on_behalf_of: <platform-admin-reader agent_id>
              scopes:       [internal:read, platform:admin]
            via Auth POST /v1/service-tokens. The forwarded user JWT in
            `X-Forwarded-Agent-JWT` carries the admin's identity for audit.
         c. BFF calls Memory service with that service JWT.
         d. Memory's standard tenant handler does `SET LOCAL app.tenant_id = T`;
            RLS allows the read; the admin sees tenant T's memories.
         e. Memory's outbox emits a `cypherx.audit.event` entry with
            `actor=platform-admin-reader`, `on_behalf_of=<px0 user_id>`,
            `target_tenant=T`, `action=memory.read` (Phase 11 audit pipeline).
    3. Same pattern reusable for any cross-tenant admin read on any service.
       Documented once here; Phase 8 already uses it for Skills.

  Why NOT just give platform:admin the ability to bypass RLS:
    - RLS bypass would require a separate Postgres role with elevated grants for
      every service — multiplies grants, multiplies audit gaps.
    - The Contract 13 promise is "cross-tenant access is architecturally impossible";
      the mint-with-target-tenant pattern keeps that promise while providing the
      operational ability admins need.
```

**RAG Dashboard (📋 — moved out of the first release; gated on Phase 5 RAG service; the test-query "Acting as agent" picker uses the Phase 11 `agent:mint_on_behalf` path — see Amendment Log):**
```
Panels:
  - Knowledge base list (table: name, doc count, chunk count, last updated)
    (doc/chunk counts come from RAG status endpoint, which counts on demand —
     Phase 5 post-edit dropped the cached counter columns to avoid races.)
  - Ingestion status (per-document progress bars; long-poll GET /v1/knowledge-bases/{id}/status)
  - Test query console (enter text → BFF /bff/knowledge-bases/{id}/query →
                        BFF mints an agent JWT for a UI-test agent of the user's choice
                        via the `agent:mint_on_behalf` path (Component 2), then proxies
                        to RAG /v1/knowledge-bases/{id}/query with that minted JWT in
                        `X-Forwarded-Agent-JWT`. UI shows an "Acting as agent: <name>"
                        dropdown so the user picks WHICH of their agents the test runs
                        as — required because RAG's RLS gates by `tenant_id` and the
                        agent JWT carries the audit trail (on_behalf_of = user).)
  - Retrieval latency chart (Prometheus)
```

---

### Component 6 — API Key Management ⚡

> **Agent API keys, NOT user developer keys.** The prior draft hedged ("user-level developer API keys... or agent API keys — architecture TBD"). Resolved: this UI manages **agent API keys** (Phase 2 `auth.api_keys`). User-level developer keys, if needed, live in px0's own UI — not here. This removes the ambiguity that would otherwise produce two parallel implementations.

```
Agent API Key section (per agent, scoped by tenant):
  - List keys for {agent_id} (show: name, key_prefix, scopes, created_at,
                              last_used_at, status; raw key NEVER shown)
  - Issue new key (modal: name, scopes, optional expiry):
      BFF calls auth-service POST /v1/agents/{agent_id}/keys
      Response includes the raw key ONCE — shown in a modal with "Copy" button
      and "I have saved this key" confirmation before close.
  - Revoke key (confirm modal):
      BFF calls auth-service DELETE /v1/agents/{agent_id}/keys/{key_id}
  - Rotate key (📋 — Phase 2's rotate endpoint is 📋)

Scope gating:
  Requires the session principal to hold agent:admin OR platform:admin on the agent's
  tenant (⚡ platform-credential mode: the session admin agent's scopes; 📋 px0 mode:
  user JWT claims). BFF enforces; auth-service re-verifies.
```

---

### Security Headers (CSP / HSTS / CSRF — MANDATORY)

A web UI consuming platform-wide APIs without these headers is the easiest path to platform-wide compromise. CSP and HSTS apply to every response from the UI origin; CSRF protection applies to every state-changing BFF call.

> **Compose parity (⚡ — see Amendment Log):** all hostnames below are the cloud form. Every origin in these headers is env-driven; in the first cycle the only origin is the BFF's own compose-published host, and the billing `frame-src` entry exists only once the px0 iframe lands (📋 Phase 11). The header SET itself ships ⚡ unchanged.

```
Content-Security-Policy:
  default-src 'self';
  script-src  'self';                          # NO 'unsafe-inline' or 'unsafe-eval'
  style-src   'self' 'unsafe-inline';          # tailwind-style component libs need inline
  img-src     'self' data: https://*.cypherx.ai;
  font-src    'self' data:;
  connect-src 'self' https://app.cypherx.ai;   # SPA only talks to its own origin (BFF)
  frame-src   https://billing.px0.cypherx.ai;  # billing iframe whitelist
  frame-ancestors 'none';                      # nothing embeds us
  base-uri    'self';
  form-action 'self';
  upgrade-insecure-requests;

Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
X-Content-Type-Options:    nosniff
X-Frame-Options:           DENY                # belt-and-braces alongside CSP frame-ancestors
Referrer-Policy:           strict-origin-when-cross-origin
Permissions-Policy:        camera=(), microphone=(), geolocation=()
Cache-Control:             no-store              # for /bff/* responses (session bound)

CSRF (double-submit):
  - On login, BFF sets two cookies:
      session=<opaque>; HttpOnly; Secure; SameSite=Strict; Path=/
      csrf=<random>;    Secure;            SameSite=Strict; Path=/    (NOT HttpOnly)
  - SPA reads csrf cookie + sends X-CSRF-Token header on every POST/PUT/PATCH/DELETE.
  - BFF rejects mismatched/missing tokens with 403 + Prometheus counter
    csrf_violations_total{route}.
```

### Origin / CORS / CSRF Architecture

> **Compose parity (⚡ first cycle — see Amendment Log):** there is exactly ONE
> browser-reachable origin: the BFF container, which serves both the SPA's static export
> and `/bff/*`. Backend services are reachable only on the internal compose network — no
> Kong, no `api.cypherx.ai`, no cross-origin surface exists, so NO CORS configuration is
> needed at all in the first cycle. SameSite=Strict cookies + CSRF double-submit apply
> unchanged. Everything below is the CLOUD (full-enterprise deploy-target) form, retained
> for the infra phase.

```
Public surface (single origin from browser's POV):
  https://app.cypherx.ai           ← CloudFront, serves static SPA
    /                               → CloudFront origin: S3 bucket cypherx-frontend-<env>
    /bff/*                          → CloudFront origin: ALB → frontend-bff service in K8s
    (no /v1/* exposure from app.cypherx.ai)

Backend surface (browser NEVER hits directly):
  https://api.cypherx.ai            ← ALB → Kong → backend services
    Only frontend-bff and SDK clients call this.

    Kong CORS plugin config (api.cypherx.ai):
      plugins:
      - name: cors
        config:
          origins:           ["https://app.cypherx.ai"]   # explicit, not "*"
          credentials:       false                         # ← the critical line
          methods:           [GET, POST, PUT, PATCH, DELETE, OPTIONS]
          headers:           [Authorization, Content-Type, X-Request-ID, Idempotency-Key,
                              traceparent, tracestate]
          exposed_headers:   [X-Request-ID, Idempotent-Replay, Retry-After,
                              X-RateLimit-Remaining, X-RateLimit-Reset, Sunset, Deprecation]
          max_age:           3600
          preflight_continue: false

    Why this blocks browser-originated calls from app.cypherx.ai:
      - app.cypherx.ai cookies (session, csrf) are SameSite=Strict — they are NOT sent
        cross-origin to api.cypherx.ai at all.
      - Even with `credentials: 'include'` from JS, Kong sets
        `Access-Control-Allow-Credentials: false` in the preflight response, so the
        browser refuses to send the request with cookies attached.
      - The ACAO listing of app.cypherx.ai means the preflight succeeds for
        Bearer-token requests (no cookies) — that's intentional for any future
        in-browser SDK use case, but the SPA itself never goes this path.

    Server-to-server callers (SDKs, the BFF) ignore CORS — those headers are
    browser-enforced only. SDK Bearer auth works as designed.

    NEVER use `Access-Control-Allow-Origin: NONE` — it is not a valid CORS value.
    The combination above (explicit origin allowlist + `credentials: false`) is the
    only correct way to express "browser cannot cookie-auth this surface".

Result:
  - Browser → app.cypherx.ai (same origin) → no CORS preflights for /bff/*.
  - Browser → api.cypherx.ai with cookies → blocked at the SameSite layer (cookies
    never sent) AND at the CORS layer (`credentials: false` rejects).
  - Browser → api.cypherx.ai with a Bearer token (no cookies) → permitted by the
    CORS rule, but the SPA has no Bearer token to send (user JWT lives in BFF's
    session store). So the codepath simply doesn't exist in the SPA.
```

### Hosting Decision — ⚡ first cycle: SPA served by the BFF container under compose | 📋 cloud form: S3 + CloudFront for SPA, K8s for BFF

**⚡ Compose parity (first cycle — replaces every AWS/K8s mechanism below; see Amendment Log):**

```
SPA:  static export baked into the cypherx/frontend-bff image at build time; the BFF
      serves /  (immutable hashed assets; index.html no-cache) and /bff/*.
BFF:  one compose service `frontend-bff` in the same compose file as the four
      first-cycle services. /livez /readyz /metrics per Contract 7; readiness gated on
      Valkey (session store) + Auth JWKS reachability — same hard/soft deps as the
      cloud probes below.
Env (ALL env-driven — compose service names, no Doppler, no cluster DNS):
  BFF_AUTH_PROVIDER=platform-credential
  VALKEY_URL
  AUTH_SERVICE_URL=http://auth:8080
  AUTH_JWKS_URL=http://auth:8080/.well-known/jwks.json
  XAGENT_URL=http://xagent:8080
  LLMS_GATEWAY_URL=http://llms:8080
  GUARDRAILS_SERVICE_URL=http://guardrails:8080
  SESSION_COOKIE_SECRET            (env-supplied)
  (PX0_* vars exist only in px0-oidc mode — 📋 Phase 11. RAG / MEMORY / TOOLS /
   PLATFORM_MGMT URLs are added as Phases 5–8/11 land.)
```

**📋 Cloud form (deploy-target — conditional on the infra phase):**

```
SPA (the static React/Next.js export):
  Built artefact:    out/ directory (static HTML/JS/CSS)
  Hosted on:         S3 bucket cypherx-frontend-<env>, fronted by CloudFront
  Cache strategy:    immutable hashed assets; index.html no-cache
  Deploy:            GitHub Actions → S3 sync → CloudFront invalidation of /index.html
  Why not K8s/SSR:   SPA is an internal admin UI — no SEO needs, no per-request server
                     rendering, no streaming HTML. K8s SSR adds a pod-fleet, an SSR cache,
                     and an attack surface for negligible benefit. SPA + BFF wins on
                     simplicity and cost (CDN cache hits dominate; backend traffic is
                     only for /bff/* dynamic calls).

Frontend BFF (the server-side session + JWT-minting layer):
  Namespace:   frontend-bff
  Deployment:  frontend-bff
  Replicas:    min 2, max 6 (HPA on CPU 70%)
  Node selector: node-role: core
  Image:       cypherx/frontend-bff (Phase 1 ECR list extended)

  Resources:
    requests: { cpu: 300m, memory: 384Mi }
    limits:   { cpu: 1000m, memory: 768Mi }

  Startup probe (Auth JWKS warm-up + Valkey session-store reach):
    startupProbe:
      httpGet: { path: /readyz, port: 8080 }
      periodSeconds: 5
      failureThreshold: 12          # 60s grace

  Health probes (Contract 7):
    livenessProbe:
      httpGet: { path: /livez, port: 8080 }
      periodSeconds: 10
    readinessProbe:
      httpGet: { path: /readyz, port: 8080 }
      periodSeconds: 5
      # Hard deps (fail readiness):
      #   - Valkey (session store; loss of sessions = mass relogin)
      #   - Auth JWKS reachable (user JWT verify)
      # Soft deps:
      #   - Backend services (per-route failure surfaces in UI; not BFF readiness)

  Env vars (from Doppler):
    VALKEY_URL                   (session store)
    AUTH_SERVICE_URL             (http://auth-service.shared-core.svc.cluster.local:8080)
    AUTH_JWKS_URL                (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
    PX0_JWKS_URL                 (https://auth.px0.cypherx.ai/.well-known/jwks.json
                                  — for verifying user JWTs)
    PX0_OIDC_CONFIG_URL          (https://auth.px0.cypherx.ai/.well-known/openid-configuration)
    PX0_OIDC_CLIENT_ID           (Doppler: frontend-bff/px0_client_id)
    PX0_OIDC_CLIENT_SECRET       (Doppler: frontend-bff/px0_client_secret)
    SERVICE_BOOTSTRAP_SECRET     (Contract 12; from service-auth/frontend-bff/bootstrap_secret)
    SESSION_COOKIE_SECRET        (Doppler: frontend-bff/session_cookie_secret — signed cookies)
    XAGENT_URL                   (http://xagent.xagent.svc.cluster.local:8080)
    LLMS_GATEWAY_URL             (http://llms-gateway.shared-core.svc.cluster.local:8080)
    GUARDRAILS_SERVICE_URL       (http://guardrails-service.shared-core.svc.cluster.local:8080)
    RAG_SERVICE_URL              (http://rag-service.shared-core.svc.cluster.local:8080)
    MEMORY_SERVICE_URL           (http://memory-service.shared-core.svc.cluster.local:8080)
    TOOL_REGISTRY_URL            (http://tool-registry.tools.svc.cluster.local:8080)
    PLATFORM_MGMT_URL            (http://platform-service.platform-mgmt.svc.cluster.local:8080)
```

### Cross-phase resources owned by Phase 12

Phase 12 owns the SPA + BFF. The cross-service Auth additions the prior draft owned
here have been rescoped (see Amendment Log): the first-cycle BFF authenticates as
tenant agents via the platform-credential session and needs NO Auth-side seeds, no
service_acl rows, and no new scopes.

**1. Migrations directory — `platform-migrations/phase-12/`:** (moved to Phase 11 — see
Amendment Log). The package — frontend-bff `auth.service_acl` seed, `agent:mint_on_behalf`
scope registration, and the `platform-admin-reader` well-known agent
(`00000000-0000-0000-0000-0000000000a3`) — ships with Phase 11's platform-migrations,
alongside the px0 integration it exists to serve.

**2. Terraform module — `terraform/modules/frontend/`:** (📋 — cloud form / deploy-target
only, conditional on the infra phase; the compose deployment needs none of it — the BFF
container serves the SPA.)

```
Resources provisioned per env (dev / staging / prod):
  - S3 bucket            cypherx-frontend-<env>
                         (SSE-S3, versioning enabled, public access blocked,
                          PR-preview prefix lifecycle 30 days)
  - CloudFront distro    cypherx-frontend-<env>-cf
                         (origin: S3 bucket via OAC; cache behaviour: immutable
                          hashed assets long TTL, /index.html no-cache,
                          /service-worker.js no-cache; SPA fallback to /index.html
                          for 404s — needed for client-side routing)
  - ACM cert             us-east-1 (CloudFront requirement) for app.<env>.cypherx.ai
                         + apex alias `app.cypherx.ai` in prod only (per Phase 1's
                          per-env hostname convention)
  - Route53 record       app.<env>.cypherx.ai → CloudFront distribution;
                         prod also gets app.cypherx.ai ALIAS → app.prod.cypherx.ai
  - IAM role             CypherX-FrontendDeployerRole (per env)
                         Trust:    GitHub OIDC for the frontend repo
                         Allows:   s3:PutObject, s3:DeleteObject, s3:ListBucket on the
                                   bucket only; cloudfront:CreateInvalidation on this
                                   distribution only.
                         (Same pattern as Phase 1 Component 18 GitHubActionsRole; this
                          role is the deploy-time equivalent for SPA artefacts.)

BFF requires NO IRSA — it makes no AWS API calls. Doppler-injected env vars + cluster
DNS to other services + Valkey for session storage cover all its dependencies.

Module owner: Platform team. Module added under terraform/modules/ in the platform
infra repo; called by environments/<env>/frontend/terragrunt.hcl.
```

**3. px0 OIDC client registration (manual, pre-deploy):** (📋 — Phase 11; px0 is
deferred and unprovisionable in the first cycle. Required only before the first
`px0-oidc`-mode BFF deploy.)

```
The OIDC client MUST be pre-registered in px0 by the platform operator BEFORE the
first BFF deploy in any env. This is currently a manual ticket to the px0 ops team;
Phase 11 (px0-bridge) will eventually expose a registration API for full automation.

Registration parameters per env:
  client_id:        <generated by px0>
  client_secret:    <generated by px0; stored in Secrets Manager at
                     cypherx/oidc/frontend-bff/<env>, KMS-encrypted, retrieved by
                     Doppler operator and injected as PX0_OIDC_CLIENT_SECRET env var>
  redirect_uris:    [https://app.<env>.cypherx.ai/bff/auth/callback]
                    (prod adds the apex alias https://app.cypherx.ai/bff/auth/callback)
  allowed_scopes:   openid profile email cypherx:*
  token_endpoint_auth_method:  client_secret_basic
  response_types:   [code]
  grant_types:      [authorization_code, refresh_token]

Operational notes:
  - Rotation: client_secret rotated every 180 days via the px0 console + Secrets
    Manager update + BFF pod restart (reloader catches the env-var change). Runbook
    📋 at docs/runbooks/px0-oidc-rotation.md.
  - The operator who ran the registration MUST capture their name + the timestamp
    in the env's infra changelog. This is the only platform credential whose
    bootstrap touches a human; same convention as Phase 1's Doppler bootstrap.
  - DO NOT commit client_secret to Git, Doppler, or any version-controlled config.
    It lives only in Secrets Manager (rotated) and the px0 console (canonical).
```

### Build / Deploy

```
⚡ First cycle (GitLab CI — the polyrepo host; there are no GitHub Actions):
  GitLab CI → npm ci → npm run build → static export copied into the frontend-bff
  docker image → push to the GitLab container registry → compose pulls the tag.
  PR previews: GitLab review-app job serving the same static build (no S3/CloudFront).

📋 Cloud form (deploy-target — conditional on the infra phase):
  SPA: CI → S3 sync cypherx-frontend-<env>
       → CloudFront invalidation of /index.html and /service-worker.js
       Pull-request previews: deploy to s3://cypherx-frontend-preview/<pr-number>/ with
       a CloudFront behaviour rewriting that path.
  BFF: CI → docker build → registry push → gitops repo PR (cypherx-gitops-bot)
       → ArgoCD sync to K8s.
  Auth on SPA build pipeline:
       cloud CI assumes the frontend deployer role via OIDC (no static keys) — the
       GitHub-Actions wording in the prior draft is replaced by GitLab CI's OIDC
       equivalent (id_token → AWS role assumption).
```

---

## ⚡ First-Release Checklist (first-cycle runtime: compose + Neon + Valkey + Redpanda + MinIO — NO AWS/K8s/Kong/px0)

- [ ] Frontend framework + component library architecture planned separately
- [ ] **BFF pattern** (`frontend-bff` compose service) with server-side session store in Valkey; SPA never sees JWTs; httpOnly session cookie + double-submit CSRF cookie
- [ ] **Pluggable auth provider** (`BFF_AUTH_PROVIDER`) — first cycle `platform-credential`: tenant_id + admin agent API key validated via the built Auth `POST /v1/agents/{id}/token` exchange; credentials stored server-side only; short-lived JWT re-mint on demand; `GET /bff/me` contract provider-agnostic so px0 OIDC slots in later unchanged
- [ ] **BFF trust boundary** — strip client-supplied `Authorization` / `X-Tenant-ID` / `X-Forwarded-Agent-JWT` / `X-Request-ID` on every inbound request; inject `X-Tenant-ID` from the session + a fresh `X-Request-ID` per proxied call; propagate `traceparent`
- [ ] **CSP + HSTS + CSRF + security-header policy** on every UI-origin response (env-driven origins; the single-origin compose deployment needs NO CORS config); `csrf_violations_total` counter
- [ ] **Hosting (compose parity)**: SPA static export served by the BFF container; all service URLs env-driven (compose service names); GitLab CI build → GitLab container registry → compose pull
- [ ] **`/livez`, `/readyz`, `/metrics`** on BFF (Contract 7); startup grace; readiness gated on Valkey + Auth JWKS
- [ ] Agent Builder — **two-step publish** (Auth identity, then xAgent runtime via `GET/PUT /v1/agents/{id}/runtime` — **WP08**); model dropdown rendered from live `GET /v1/models` (**WP05**); `memory_scope` enum from the live runtime API incl. `session` (never hardcoded; `global` invalid); tools/skills selectors hidden until Phases 7/8
- [ ] Agent List + edit — Auth `GET /v1/agents` (list) + `PATCH /v1/agents/{id}` (**WP04** — named dependency; not buildable ahead of it)
- [ ] **Agent test runner** — worst-case cost banner BEFORE run (client-side: `token_budget_per_task` × model pricing from `GET /v1/models`); ACTUAL cost shown post-run; `metadata.test=true` on submission; agent JWT via the Component 2 API-key → token exchange (key held only in the Valkey session, never in the browser); `body.agent_id` = JWT agent_id per the Phase-9 amendment
- [ ] **Task feed: long-poll** via xAgent `GET /v1/tasks` list (**WP08** — named dependency); task detail timeline — step-name discriminator matches Phase 9 `xagent.task_steps.step_name` (`guardrail_check_input`, `llm_call`, etc.)
- [ ] Auth Dashboard — agents, agent API keys, **paginated audit log** (`GET /v1/audit-log?agent_id=&event_type=&from=&to=&limit=50&cursor=` — path corrected per Amendment Log); never bulk-loads `auth.audit_log`
- [ ] LLMs Dashboard — usage/cost via `GET /v1/usage`, model metadata via `GET /v1/models` (**WP05** — named dependencies); cost breakdown includes `cached_prompt_tokens` + `cache_creation_tokens` from Phase 3 post-edit
- [ ] Guardrails Dashboard — violation log via `GET /v1/violations` (**WP07** — named dependency; displays redacted `matched` strings only per Phase 4 post-edit); **policy editor as version-publisher** ("Publish new version", version history + revert; never in-place edit)
- [ ] **Agent API Key management** (NOT user developer keys) — issue, list, revoke; raw key shown once in modal with copy + confirm

## 📋 Full Enterprise Implementation Checklist

**Service code (📋 — gated on the named owning phase):**

- [ ] px0 SSO provider (`px0-oidc`) in BFF (NOT in SPA); refresh tokens held server-side; same `/bff/me` contract as first cycle (Phase 11)
- [ ] **px0 OIDC client pre-registered per env** (manual handoff to px0 ops; client_secret in Secrets Manager `cypherx/oidc/frontend-bff/<env>`; redirect_uri uses env-scoped `app.<env>.cypherx.ai`) (Phase 11)
- [ ] **`agent:mint_on_behalf` scope** registered in Auth, deny-by-default, granted ONLY to `frontend-bff`; Auth `/v1/agents/{id}/token` accepts it when the forwarded user JWT holds `agent:admin` — together with the `platform-migrations` package (service_acl seed, scope registration, `platform-admin-reader` agent `...00a3`) (moved to Phase 11 — see Amendment Log)
- [ ] Test-runner upgrade: BFF mints agent JWT via `agent:mint_on_behalf` (replaces the first-cycle key paste); minted JWT carries `on_behalf_of = user_id` for audit; never reaches browser (Phase 11)
- [ ] Multiplexed tenant-wide SSE task feed (gated on xAgent multiplex endpoint)
- [ ] **Workflow canvas GATED on Phase 10** — project plan reflects dependency; workflow live execution view lands with it
- [ ] **Memory Dashboard admin-only** — scope `platform:admin` only (NOT `memory:admin` — undefined); UI filters honour Phase 6 ownership rules (Phase 6)
- [ ] **Cross-tenant admin reads** — BFF mints service JWT with `tenant_id = <target>` + `on_behalf_of = platform-admin-reader agent_id`; Memory's tenant handler sets `app.tenant_id` to target; RLS gates naturally; audit records the px0 user as actor (Phases 6 + 11)
- [ ] RAG Dashboard (knowledge bases, test query console — "Acting as agent: X" picker via `agent:mint_on_behalf`); doc/chunk counts via on-demand RAG status endpoint (Phases 5 + 11)
- [ ] Skill Library (browse, preview, search) — Contract 11 post-edit JSON Schema rendering (Phase 8)
- [ ] Skill YAML editor — in-browser validation against same JSON Schema as Phase 8 CI; template helper highlighting (platform allow-list only) (Phase 8)
- [ ] Tool Registry browser — per-tool **version dropdown**, deprecation banner + sunset date, test console via BFF (Phase 7)
- [ ] Team management (invite, roles) — proxies to px0 (Phase 11)
- [ ] Billing overview (embedded from px0) — CSP `frame-src` whitelist (Phase 11)
- [ ] Responsive design (desktop-first, tablet support)

**Deploy-target (📋 — cloud form, conditional on the infra phase; compose equivalents already shipped ⚡):**

- [ ] **Correct CORS config on `api.cypherx.ai`** — Kong plugin with explicit `origins: ["https://app.cypherx.ai"]` AND `credentials: false` (NOT the invalid `Access-Control-Allow-Origin: NONE`); browser cookie-auth structurally impossible (compose equivalent: single-origin BFF, no CORS surface exists)
- [ ] **Hosting**: S3 + CloudFront for SPA; K8s deployment for BFF; documented build/deploy paths (compose equivalent: SPA-in-BFF container)
- [ ] **Terraform `terraform/modules/frontend/`** — per-env S3 bucket, CloudFront distribution (with /index.html SPA-fallback), ACM cert in us-east-1, Route53 records (env-scoped + prod apex alias), frontend deployer IAM role for CI OIDC SPA deploys; BFF needs NO IRSA
- [ ] Deployed via cloud CI/CD (GitLab CI OIDC → S3 + CloudFront for SPA; ArgoCD for BFF) (compose equivalent: GitLab CI → registry → compose pull)

📋 deferred (post-first Phase 12 release):
- Workflow canvas (gated on Phase 10)
- Multiplexed tenant-wide SSE task feed (gated on xAgent multiplex endpoint)
- Diff view for agent / skill / policy versions
- Mobile-responsive design (tablet-only first)
- Per-tenant theming / white-label

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. BFF Becoming a "God Service" — REAL
Evidence: lines 35–40, 596–607 (BFF proxies 8 services, broad `internal:read/write` scopes).
**Mitigation:** BFF is JWT-exchange + session layer only. No tenant aggregation, no ACL transforms, no application logic. All downstream services enforce their own RLS + tenant gating; BFF proxies faithfully.

### 2. `agent:mint_on_behalf` Scope Hardening — PARTIAL
Evidence: lines 184–195, 220–224 (scope deny-by-default; recipient constraint missing).
**Mitigation:** `Forwarded-User-JWT` in mint step MUST carry `agent:admin` on the agent's tenant AND originate from px0 (verified via Auth JWKS). Cross-tenant minting rejected by xAgent RLS when `agent_id` differs from tenant.

### 3. Cross-Tenant Admin Read Pattern — ALREADY-ADDRESSED
Evidence: lines 340–376. Comprehensive design (pre-provisioned platform-admin-reader, target-tenant JWT minting, handler isolation, audit).

### 4. Missing Distributed Rate Limiting Strategy — NOT-REAL
Rate limiting enforced at Kong (lines 468–479), not BFF; BFF is stateless. Not a frontend architecture risk.

### 5. Workflow Canvas Dependency Gating — ALREADY-ADDRESSED
Evidence: lines 267–269, 714. Explicitly gated on Phase 10; not in first Phase 12 release.

### 6. Valkey as Critical Session Dependency — REAL
Evidence: lines 544–550 (hard dependency for readiness; no degraded-mode).
**Mitigation:** Valkey cluster 3+ replicas with PVs + snapshots (Phase 1 infra); connection-loss alert with 5 min SLO to restore. Sticky-session failover to replica tracked 📋 post-launch.

### 7. Dashboard Aggregation Scalability — PARTIAL
Evidence: lines 293–394 (multiple backend hits; concurrency unspecified).
**Mitigation:** BFF serializes (not parallelizes) per-panel calls under peak load; 30 s per-tenant BFF cache reduces cascading load on repeated dashboard renders.

### 8–12. SPA Never Sees JWT / Long-Poll First / Versioning / CORS-SameSite / Tenant Boundary Integrity — ALL VERIFIED
Evidence: lines 45/105, 54/231, 148/166, 455–506, 233/345–376. All well-designed.
