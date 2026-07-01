# Phase 9 — xAgent Core
> **Status:** ⏳ Pending | **Depends On:** Phase 2, 3, 4 (first cycle) / Phase 5, 6, 7, 8 (enhanced)
> **First Cycle:** ⚡ Partial — single-agent runtime with LLM + Guardrails integration required

## Amendment Log (2026-06 — pre-build reconciliation)

- **Contract-15 gating pinned to cases 1–10.** Contract 15 defines 15 cases; the old "all 10 cases" wording mis-stated the gate. Cases 11–15 gate the enterprise wave (WP12/WP14), not 9A. Added the previously missing ⚡ item for Contract-9 idempotency on `POST /v1/tasks` (Contract-15 cases 12/13 target xAgent but appeared in no checklist — duplicate task submissions double LLM spend).
- **Caller-vs-target agent authorization defined (live-bug fix):** first cycle requires `body.agent_id == jwt.agent_id`; mismatch → 422 `VALIDATION_ERROR`. Cross-agent invocation arrives only via 9B A2A delegation tokens. (Code previously persisted `body.agent_id` while executing the JWT agent's config — task rows, events and cost mis-attributed to an agent whose config never ran.)
- **`cypherx.memory.write.requested` topic DELETED:** Phase 6 never defined or consumed it (writes would vanish). The MEMORY_WRITE stage (📋) now calls Memory `POST /v1/memories` directly — async fire-and-forget HTTP with service JWT + `X-Forwarded-Agent-JWT` — reusing Memory's validation + idempotency.
- **Auth `/v1/authorize` layer-B cadence specified:** task submission checks a Valkey-cached authorize verdict (key `tenant_id`+`agent_id`, action `task:execute`, 60s TTL); fail-closed on deny, fail-open + alert on Auth outage. Previously the "stateful checks" had no trigger/cadence and suspended tenants executed until JWT expiry.
- **Timeout sweeper atomicity fixed:** the sweeper marks `status='timeout'` AND inserts the `task.failed` outbox row in ONE transaction — the old spec omitted the outbox row, breaking Component 3b's own invariant.
- **Runtime config no longer create-only:** added `GET`/`PUT /v1/agents/{id}/runtime` (PUT bumps `runtime_version`) plus explicit status transitions. The old `ON CONFLICT DO NOTHING`-only POST silently returned old config, contradicting the all-config-dynamic rule.
- **`user_id` semantics tightened:** set ONLY from explicit caller input — the JWT-`sub` fallback is removed (it conflated agent identity with end-user identity); column stays nullable until Phase 6 defines an explicit `user_ref`.
- **MCP client (Component 4) relabeled 📋** (was mislabeled ⚡) — all consumers are Phase 7; it lands with TOOL_LOOP in WP12.
- **Codified:** `xagent.tasks.metadata` JSONB column; `redact → passed` step-status mapping (Component 6); readiness gates Postgres + Auth JWKS ONLY (blesses the code's divergence); `DELETE`-cancel semantics (202 pending/running, 409 terminal, 404 under RLS, 503 on Valkey down; submitting agent or `agent:admin` only).
- **Optional `input.session_id` added** (Contract 3 task input) with a matching `xagent.tasks.session_id` column; MEMORY stages (📋) register sessions idempotently via Memory `POST /v1/memories/sessions` before first session-scope use.
- **Checklist hygiene (compose-parity):** ⚡ deploy-target items (sweeper CronJob, K8s probes/resources, ArgoCD deploy) restated as compose-runtime equivalents; the K8s forms remain documented as the cloud deployment shape.
- **RAG_QUERY call shape aligned to Phase 5's path-param contract (📋 stage pseudocode):** `POST /v1/knowledge-bases/{kb_id}/query` iterated per `allowed_kb_ids`, body `{query, top_k, min_score}` with `top_k <= 20` as the client-side cap — `kb_id` travels in the path, never in the body (the old pseudocode put it in the body, which Phase 5's endpoint does not accept).

---

## ⚠️ Scope Boundary (read first)

The earlier draft of this phase listed "Tool-use loop (MCP client)" and "Memory injection" as first-cycle items. **They are explicitly removed from first cycle** because their dependencies (Phase 5 RAG, Phase 6 Memory, Phase 7 Tools) are all 📋.

The **first-cycle execution engine is intentionally a simplified path**:

```
LOAD agent → PRE-GUARDRAIL → LLM call (single round-trip, no tools) → POST-GUARDRAIL → RETURN
```

No memory retrieval. No tool calls. No skill loading. No RAG context. The execution engine **must be structured** so these can be added later without re-architecting — i.e., the LLM call is a single step in a pipeline, not a hard-coded function. The Phase 9A code lays the rails; the Full Enterprise checklist fills them in.

This makes the **First Cycle Smoke Test (Contract 15)** the explicit exit criterion: **cases 1–10** of the 15 cases Contract 15 defines — no tools, no memory, no RAG, no skills — just safe LLM round-trips with audit and observability. Cases 11–15 gate the enterprise wave (WP12/WP14), not 9A.

---

## Phase Overview

xAgent is the **heart of the platform** — the runtime that executes agents, calls tools via MCP, loads skills, integrates all SharedCore services, and returns results to callers. This is the largest and most complex phase; it is built in sub-phases.

**Sub-phases:**
- **9A** — Single-Agent Runtime (first cycle ⚡)
- **9B** — A2A Communication (after first cycle)
- **9C** — Orchestration Engine (after first cycle)

**Deliverable:** A running xAgent service that accepts task submissions, integrates with Auth/Guardrails/LLMs, can invoke MCP tools, and returns results — first synchronously, then with streaming.

> 🏗️ **Service Architecture Note:** The internal architecture of the xAgent runtime (task execution engine, MCP client library, skill execution engine, state machine, streaming implementation) must be planned separately before implementation begins. Each sub-phase (9A, 9B, 9C) should have its own detailed service architecture plan.

---

## High Level Design

### System Context

```
                        ┌──────────────────────────────────────────────┐
                        │              xAGENT                           │
                        │                                              │
  External Client ─────►│  POST /v1/tasks          (submit task)      │
  Kong Gateway ─────────│  GET  /v1/tasks/{id}     (get status)       │
                        │  GET  /v1/tasks/{id}/stream (SSE stream)    │
                        │  GET  /v1/capabilities                       │
                        │                                              │
                        │  (9C) POST /v1/workflows  (submit workflow)  │
                        └──────────────┬───────────────────────────────┘
                                       │ calls
          ┌──────────────┬─────────────┼─────────────────┬─────────────┐
          ▼              ▼             ▼                  ▼             ▼
   Auth Service   Guardrails    LLMs Gateway        Memory Svc    Tool Registry
   (validate JWT   (check I/O)  (all LLM calls)    (read/write   (discover
    + authorize)                                    memories)     tools)
                                                        │
                                                        ▼
                                                 MCP Tool Servers
                                                 (invoke via MCP)
                                                        │
                                                        ▼
                                               Skill Retriever
                                               (find skills)
```

### Agent Execution Lifecycle

```
Task submitted: POST /v1/tasks
  │
  ▼
1. Kong verifies JWT signature + exp/aud (edge layer).
2. xAgent re-verifies the agent JWT locally via JWKS (defense in depth, Phase 2 layer-A);
   checks scopes locally (agent:execute); extracts tenant_id, agent_id.
   Layer-B stateful checks (tenant suspended, plan-tier gate, budget hard-stop) ARE
   consulted on every task submission — via a Valkey-cached Auth /v1/authorize verdict:
   key authz:{tenant_id}:{agent_id}, action task:execute, 60s TTL. Cache miss → ONE
   Auth /v1/authorize call; cached verdict otherwise. Deny → fail closed (403).
   Auth outage → fail open + alert. This preserves the Phase 2 layer-A (always-local)
   / layer-B (on-demand) split — no synchronous Auth round-trip on the hot path beyond
   one call per (tenant, agent) per 60s.
3. Validate task input schema (size cap 256 KiB; see Component 2); enforce
   body.agent_id == jwt.agent_id → 422 VALIDATION_ERROR on mismatch (Component 2).
4. Load agent definition (from PostgreSQL / Valkey cache).
5. [PRE-LLM]  Check input with Guardrails (service-JWT + X-Forwarded-Agent-JWT auth).
6. Retrieve relevant memories (Memory service) — 📋 enhanced pass only.
7. Query relevant skills (tool-skill-retriever) — 📋 enhanced pass only.
8. Build prompt: system_prompt + memories + skill_instructions + user_input.
9. Call LLMs Gateway /v1/chat/completions.
   ├── If response is a tool call → execute tool via MCP → loop back to step 9 — 📋 enhanced only.
   └── If response is final answer → continue.
10. [POST-LLM] Check output with Guardrails.
11. Store significant memories (Memory service, async) — 📋 enhanced pass only.
12. Publish task.completed (or task.failed) event via outbox (Kafka).
13. Return response to caller.
```

> **Agent JWT forwarding rule (CRITICAL — applies to every downstream call):**
> xAgent does NOT mint agent JWTs. It captures the inbound `Authorization: Bearer <agent-jwt>` (already verified by Kong + locally re-verified at step 2) and forwards it verbatim via the `X-Forwarded-Agent-JWT` header on every downstream call to Auth, Guardrails, LLMs, RAG, Memory, Tool Registry, and tool MCP servers. The `Authorization` header on those outbound calls carries **xAgent's own service JWT** (Contract 12, minted via `service-auth/xagent/bootstrap_secret`). Downstream services re-verify both JWTs locally via JWKS — no central auth round-trip needed. `traceparent` is also forwarded on every hop (Contract 8).

---

## Low Level Design

> **INSTRUCTION:** All components below must be fully designed before implementation begins.
> ⚡ items implement first. 📋 items design now, implement after first cycle.

---

## Sub-Phase 9A — Single-Agent Runtime ⚡

### Component 1 — Agent Definition ⚡

**What it is:** The config file that defines an agent's identity, capabilities, and behaviour.

**PostgreSQL (`xagent.agents`):**
```sql
CREATE TABLE xagent.agents (
  agent_id              UUID         PRIMARY KEY,
                        -- same UUID as auth.agents.agent_id; NO cross-schema FK
                        -- (Phase 1 per-schema users prevent it). Cross-service validation
                        -- via Auth GET /v1/agents/{id} at insert time.
  tenant_id             UUID         NOT NULL,
  name                  VARCHAR(255) NOT NULL,
  runtime_version       VARCHAR(50)  NOT NULL DEFAULT '1.0.0',
                        -- distinct from auth.agents.version: that's the identity revision;
                        -- this is the runtime-config revision. Both can move independently.
  status                VARCHAR(20)  NOT NULL DEFAULT 'active',
                        -- active | inactive | pending_config

  -- LLM configuration
  llm_model             VARCHAR(100) NOT NULL DEFAULT 'smart',
  system_prompt         TEXT         NOT NULL,
  max_tokens            INTEGER      NOT NULL DEFAULT 2048,
  temperature           FLOAT        NOT NULL DEFAULT 0.7,

  -- Integration config
  memory_scope          VARCHAR(20)  NOT NULL DEFAULT 'agent',
                        -- none | agent | user | tenant | session
                        -- ('global' renamed to 'tenant' to match Phase 6 post-edit)
  guardrail_policy_id   UUID,                                   -- null = platform default
  allowed_tools         TEXT[]       NOT NULL DEFAULT '{}',
                        -- entries are version-pinned per Phase 7 post-edit:
                        --   "tool-web-search@1.0.0" or "tool-web-search@latest"
  allowed_skills        TEXT[]       NOT NULL DEFAULT '{}',
  allowed_kb_ids        UUID[]       NOT NULL DEFAULT '{}',
                        -- RAG knowledge bases the agent is bound to. RAG_QUERY stage
                        -- iterates this list and merges chunks into the prompt context.
                        -- KB ACL (Phase 5 Component 5c) is enforced server-side too —
                        -- this list is the agent's intent; RAG is the authority.
  rag_top_k_per_kb      INTEGER      NOT NULL DEFAULT 5,
  rag_min_score         FLOAT        NOT NULL DEFAULT 0.7,
  token_budget_per_task INTEGER      NOT NULL DEFAULT 10000,

  -- Capability advertisement (for A2A routing)
  capabilities          JSONB        NOT NULL DEFAULT '[]',
  metadata              JSONB        NOT NULL DEFAULT '{}',

  created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  CONSTRAINT memory_scope_enum CHECK (memory_scope IN ('none','agent','user','tenant','session')),
  CONSTRAINT status_enum        CHECK (status IN ('active','inactive','pending_config')),
  CONSTRAINT temperature_range  CHECK (temperature >= 0.0 AND temperature <= 2.0)
);

CREATE INDEX idx_xagent_agents_tenant ON xagent.agents(tenant_id);

-- RLS (Contract 13 — tenant-scoped):
ALTER TABLE xagent.agents ENABLE ROW LEVEL SECURITY;
CREATE POLICY xagent_agents_isolation ON xagent.agents FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **Schema name disclosure note for external/A2A interop.** The table lives in the `xagent.*` schema for historical reasons (this service is xAgent's runtime). External runtimes participating in A2A federation should NOT need to write to or know about this table — A2A receivers use the platform-neutral API `POST /v1/a2a/tasks` (Phase 10) and `POST /v1/agents/{id}/runtime` (this phase). The schema name is implementation detail. A view alias `platform.agents` is exposed by Phase 11's platform-service for any cross-tool read that should be implementation-agnostic.

**Agent runtime lifecycle (resolves the auth.agents ↔ xagent.agents provenance gap):**

| Step | Owner | Endpoint | Result |
|------|-------|----------|--------|
| 1 | Agent owner | `POST /v1/agents` on Auth (Phase 2) | `auth.agents` row created. Identity exists; no runtime yet. |
| 2 | Agent owner | `POST /v1/agents/{agent_id}/runtime` on xAgent (this phase) | `xagent.agents` row created with full runtime config. Status `active`. |
| 3 | Caller | `POST /v1/tasks { agent_id }` | If `xagent.agents` row missing → 409 `CONFLICT` `"agent runtime not configured"`. |

The xAgent runtime-config endpoint (`POST /v1/agents/{agent_id}/runtime`) at insert time:
- Validates JWT scope `agent:admin` (or `platform:admin`).
- Calls Auth `GET /v1/agents/{agent_id}` to confirm the agent exists and tenant_id matches the caller's JWT. Rejects 404/403 otherwise.
- Inserts the `xagent.agents` row.
- Idempotent on `agent_id` (returns existing row on duplicate — it NEVER silently overwrites; updates go through PUT below).

**Runtime config is read/update, not create-only (all-config-dynamic rule):**
- `GET /v1/agents/{agent_id}/runtime` ⚡ — returns the current `xagent.agents` row (same scope rule as POST).
- `PUT /v1/agents/{agent_id}/runtime` ⚡ — updates mutable runtime fields and BUMPS `runtime_version` on every successful write (same validation + scope rule as POST).
- **Status transitions (via PUT):** `active ↔ inactive`; `pending_config → active` once required fields are present. Tasks against a non-`active` runtime → 409 `CONFLICT`.

📋 follow-up: a Kafka consumer for `cypherx.auth.agent.registered` (Phase 2 ⚡ event) that auto-creates a stub `xagent.agents` row with status `pending_config` so owners only need step 2 to be "fill in the prompt".

---

### Component 2 — Task Management ⚡

**PostgreSQL (`xagent.tasks`):**
```sql
CREATE TABLE xagent.tasks (
  task_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id      UUID         NOT NULL,
                -- no FK to xagent.agents (we DO own that schema, but RLS context
                -- complicates FK validation in transaction-mode PgBouncer; validated
                -- at app layer instead).
  tenant_id     UUID         NOT NULL,
  user_id       UUID,                       -- opaque tenant-local user id (see note below)
  session_id    UUID,                       -- optional Contract-3 input.session_id (see note below);
                -- the producer for memory.sessions registration (Phase 6)
  trace_id      UUID         NOT NULL,
  status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
                -- pending | running | completed | failed | cancelled | timeout
  input         JSONB        NOT NULL,
  metadata      JSONB        NOT NULL DEFAULT '{}',
                -- free-form caller tags from the request body (reserved-key list applies);
                -- codified 2026-06 — the request schema always had it, the table didn't.
  output        JSONB,
  error_code    VARCHAR(50),
  error_msg     TEXT,
  tokens_used   INTEGER,
  cost_usd      NUMERIC(12,8),
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  started_at    TIMESTAMPTZ,
  completed_at  TIMESTAMPTZ,
  timeout_at    TIMESTAMPTZ,                -- task is cancelled if not done by this time

  CONSTRAINT status_enum CHECK (status IN ('pending','running','completed','failed','cancelled','timeout'))
);

CREATE INDEX idx_tasks_agent_id  ON xagent.tasks(agent_id, created_at DESC);
CREATE INDEX idx_tasks_tenant_id ON xagent.tasks(tenant_id, created_at DESC);
CREATE INDEX idx_tasks_status    ON xagent.tasks(status);
-- Index supporting the timeout sweeper (see Per-task timeout below):
CREATE INDEX idx_tasks_running_timeout
  ON xagent.tasks(timeout_at) WHERE status IN ('pending','running');

ALTER TABLE xagent.tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY xagent_tasks_isolation ON xagent.tasks FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

> **`user_id` semantics:** opaque tenant-local user identifier supplied by the caller. CypherX does NOT own users (per Phase 2 / Phase 6 pattern). Not validated against any registry. Agent owners interpret it. Same identifier space as `memory.memories.scope_id` when `scope='user'`. **Set ONLY from explicit caller input — there is NO JWT-`sub` fallback** (a JWT subject is the agent/principal, not an end-user; the fallback conflated the two and mis-scoped user-scope memory). Column stays nullable until Phase 6 defines an explicit `user_ref`.

**Task API:**
```
POST   /v1/tasks                       Submit task                                ⚡  (mode=sync only)
GET    /v1/tasks/{task_id}             Get task result                            ⚡
DELETE /v1/tasks/{task_id}             Cancel task (cooperative)                  ⚡
GET    /v1/tasks/{task_id}/stream      SSE stream                                 📋
GET    /v1/tasks                       List tasks (paginated)                     📋
POST   /v1/agents/{agent_id}/runtime   Register runtime config (see Component 1)  ⚡
GET    /v1/agents/{agent_id}/runtime   Read runtime config (see Component 1)      ⚡
PUT    /v1/agents/{agent_id}/runtime   Update runtime config — bumps
                                       runtime_version (see Component 1)          ⚡
```

**Task Request:**
```json
POST /v1/tasks
Authorization: Bearer <agent-jwt>           ← Kong-verified; xAgent re-verifies locally
                                              and forwards via X-Forwarded-Agent-JWT to downstream

{
  "agent_id":        "<uuid>",
  "input":           { "message": "Summarise the latest AI research trends" },
  "mode":            "sync",       -- first cycle accepts ONLY "sync";
                                   -- async/stream → 422 VALIDATION_ERROR (📋)
  "priority":        "normal",
  "timeout_seconds": 120,          -- max 900 (Contract 3 A2A parity)
  "metadata":        {}            -- free-form tags; reserved-key list applies
                                   -- (see Phase 3 Component 1 — same rule)
}
```

> **Caller-vs-target authorization (FIRST-CYCLE RULE — closes a live mis-attribution bug):** `body.agent_id` MUST equal `jwt.agent_id` — the submitting agent may invoke only ITS OWN runtime. Mismatch → 422 `VALIDATION_ERROR`, checked before any agent load or persistence. Cross-agent invocation arrives ONLY via 9B A2A delegation tokens (📋). Persisting `body.agent_id` while executing the JWT agent's config mis-attributes task rows, Kafka events and cost to an agent whose config never ran — the exact bug this rule forbids (code fix lands in WP02: `api/tasks.py` + `core/stages/load.py`).

> **Idempotency (Contract 9) ⚡:** `POST /v1/tasks` honours `Idempotency-Key` — Valkey idem key (24h TTL); replay returns the original response with the `Idempotent-Replayed` header set; same key with a different body → 409 `IDEMPOTENCY_KEY_CONFLICT`; Valkey outage → **fail CLOSED with 503** (duplicate task submissions double LLM spend, so fail-open is not acceptable here). Satisfies Contract-15 cases 12/13 (enterprise-wave gated).

> **Optional `input.session_id` (Contract 3):** a UUID inside `input`; persisted to `xagent.tasks.session_id`. When the MEMORY stages (📋) are enabled and the agent uses session-scope memory, xAgent registers the session idempotently via Memory `POST /v1/memories/sessions` BEFORE the first session-scope use. Without this field, session-scope memory has no producer.

> **Input size cap:** `input` serialised JSON MUST NOT exceed **256 KiB** (Contract 3 A2A parity). Over-cap → 422 `VALIDATION_ERROR`. Server-enforced before any other processing.

> **`timeout_seconds` cap:** MUST be in `[1, 900]` (matches Contract 3 A2A range). Anything outside → 422 `VALIDATION_ERROR`.

**Per-task timeout enforcement:**
- **Primary (in-pod):** the request handler creates a `context.WithTimeout(timeout_seconds)`. The LLM/Guardrails/Tool calls propagate cancellation. On expiry: task row → `status='timeout'`, `error_code='TIMEOUT'`, `task.failed` event emitted via outbox.
- **Backup (sweeper):** an in-process background loop, every 30 s (cloud form: K8s CronJob), marks orphaned rows where `status IN ('pending','running') AND timeout_at < NOW() - INTERVAL '30 seconds'` as `timeout` **AND inserts the corresponding `cypherx.agent.task.failed` outbox row in the SAME transaction** (Component 3b atomicity invariant — a swept task without its Kafka event is exactly the divergence the outbox exists to prevent). Captures pod-crash leftovers. Index above supports the sweep.
- Cooperative cancellation via `DELETE /v1/tasks/{task_id}` writes a `cancellation_signals:{task_id}` flag in Valkey (TTL = task remaining time); the in-pod handler polls it between stages. On detect: row → `status='cancelled'`, hard-kill the in-flight LLM call via context.Cancel.
- **`DELETE`-cancel semantics (codified):** **202** if the task is `pending`/`running` (cancellation requested, cooperative); **409** `CONFLICT` if already terminal (`completed`/`failed`/`cancelled`/`timeout`); **404** if invisible under RLS (cross-tenant — anti-existence-leak); **503** if Valkey is down (the signal cannot be durably set). Authorization: only the submitting agent or a caller with `agent:admin` may cancel.

**Task Response:**
```json
{
  "task_id":    "<uuid>",
  "status":     "completed",
  "output": {
    "message":   "AI research in 2026 focuses on..."
  },
  "tokens_used": 1450,
  "cost_usd":    0.00726,
  "duration_ms": 2340,
  "trace_id":   "<uuid>"
}
```

---

### Component 3 — Execution Engine ⚡

**What it is:** The core loop that runs an agent task end-to-end.

**Design rule:** the engine is a **pipeline of named stages**. Each stage is independently feature-flagged. First-cycle stages are `LOAD`, `PRE-GUARDRAIL`, `LLM`, `POST-GUARDRAIL`, `EVENT`, `RETURN`. Enhancement stages (`MEMORY_RETRIEVE`, `SKILL_LOAD`, `TOOL_LOOP`, `MEMORY_WRITE`) are added later via config — no rewrite needed.

```
function executeTask(task):

  context = {
    task, agent: null, policy: null,
    messages: [], tools: [], tokens_used: 0,
    final_answer: null, error: null
  }

  for stage in agent.pipeline:        // pipeline defined in agent definition
    if not stage.enabled: continue
    stage.run(context)
    if context.error: break

  return context.final_answer or context.error


Stages (⚡ = first cycle, 📋 = full enterprise):

⚡ LOAD
   context.agent  = loadAgent(task.agent_id)            // Valkey cache, 5min TTL
   context.policy = loadPolicy(agent.guardrail_policy_id)

⚡ PRE-GUARDRAIL
   // Identity/correlation flow via headers — NEVER in the body (Contract 13).
   r = guardrails.checkInput(
     headers: {
       Authorization:         "Bearer " + xagent.serviceJWT(),
       X-Forwarded-Agent-JWT: context.inboundAgentJWT,
       traceparent:           context.traceparent
     },
     body: { text: task.input.message, task_id: task.task_id }
   )
   if r.decision == "block":  context.error = GUARDRAIL_VIOLATION; return
   if r.decision == "redact": task.input.message = r.processed_text

📋 MEMORY_RETRIEVE                          (added in Phase 9 enhanced)
   // If task.session_id is set and the agent uses session-scope memory, FIRST register
   // the session idempotently: POST /v1/memories/sessions (Phase 6) — required before
   // the first session-scope read or write (Memory never lazy-creates session rows).
   memories = memory.retrieve({ ... })
   context.memory_context = formatMemories(memories)

📋 RAG_QUERY                                (added in Phase 9 enhanced, requires Phase 5)
   // For each KB the agent is bound to, retrieve top-K relevant chunks.
   // KB binding lives on xagent.agents.allowed_kb_ids (UUID[]).
   // Call shape aligned to Phase 5's path-param contract (amended — see Amendment Log):
   // POST /v1/knowledge-bases/{kb_id}/query, iterated per allowed_kb_ids — kb_id travels
   // in the PATH, NEVER in the body.
   for kb_id in agent.allowed_kb_ids:
     hits = rag.query(                            // POST /v1/knowledge-bases/{kb_id}/query
       path: { kb_id: kb_id },
       headers: {
         Authorization:         "Bearer " + xagent.serviceJWT(),
         X-Forwarded-Agent-JWT: context.inboundAgentJWT,
         traceparent:           context.traceparent
       },
       body: {
         query:     task.input.message,
         top_k:     agent.rag_top_k_per_kb,        // default 5; client-side cap top_k <= 20
         min_score: agent.rag_min_score            // default 0.7
       }
     )
     context.rag_context += formatChunks(hits.chunks, kb_id)
     context.rag_chunks_returned += len(hits.chunks)

   // KB ACL (Phase 5 Component 5c) is enforced server-side — RAG returns 403 FORBIDDEN_KB
   // if the agent (per X-Forwarded-Agent-JWT) does not have read on the kb.
   // xAgent does NOT pre-filter — defence in depth.

📋 SKILL_LOAD                               (added in Phase 9 enhanced, requires Phase 8)
   skill = tool-skill-retriever.find_skills(task.input.message, top_k=1)[0]
   if skill: context.skill_steps = skill.steps

⚡ PROMPT_BUILD
   context.messages = [
     { role: "system", content: agent.system_prompt
                              + (context.rag_context || "")
                              + (context.memory_context || "")
                              + (formatSkillInstructions(context.skill_steps) || "") },
     { role: "user",   content: task.input.message }
   ]

⚡ LLM   (single round-trip in first cycle — no tool loop)
   // First cycle is mode=sync only; stream is 📋.
   response = llms.chat(
     headers: {
       Authorization:         "Bearer " + xagent.serviceJWT(),
       X-Forwarded-Agent-JWT: context.inboundAgentJWT,
       traceparent:           context.traceparent
     },
     body: {
       model:       agent.llm_model,
       messages:    context.messages,
       max_tokens:  min(agent.max_tokens, agent.token_budget_per_task),  // first-cycle budget cap (single call)
       temperature: agent.temperature,
       tools:       [],                                                   // empty for first cycle
       stream:      false                                                  // (task.mode == "stream") is 📋
     }
   )
   context.tokens_used += response.usage.total_tokens
   context.cost_usd    += response.usage.cost_usd

   if response.choices[0].finish_reason == "stop":
     context.final_answer = response.choices[0].message.content
   else:
     context.error = "Unexpected finish_reason: " + response.choices[0].finish_reason

📋 TOOL_LOOP                                (added when Phase 7 tools land)
   while last_response.finish_reason == "tool_calls":
     tool_calls = last_response.message.tool_calls
     for tc in tool_calls:
       // tc.function.name is the tool's function name (e.g., "web_search").
       // agent.allowed_tools is a list of versioned MCP server names: "tool-web-search@1.0.0".
       // Map function name → MCP server via manifest cache; reject if not in allowed_tools.
       assert resolveOwningServer(tc.function.name) in agent.allowed_tools  // ELSE 403
       tool = toolRegistry.get(versioned-name)         // returns version-specific endpoint
       result = mcpClient.invoke(tool.endpoint,
         headers: {
           Authorization:         "Bearer " + xagent.serviceJWT(),
           X-Forwarded-Agent-JWT: context.inboundAgentJWT,
           traceparent:           context.traceparent,
           Idempotency-Key:       deriveIdempotencyKey(task_id, tc.id)   // if tool.idempotent
         },
         body: { tool: tc.function.name, input: parseJSON(tc.function.arguments) }   // NO identity
       )
       context.messages.append({ role: "assistant", tool_calls: [tc] })
       context.messages.append({ role: "tool", tool_call_id: tc.id, content: result.output })
     last_response = llms.chat({ ...same as LLM stage..., tools: agent.tools })
     context.tokens_used += last_response.usage.total_tokens
     if context.tokens_used > agent.token_budget_per_task:
       context.error = BUDGET_EXCEEDED; return
   context.final_answer = last_response.choices[0].message.content

⚡ POST-GUARDRAIL
   r = guardrails.checkOutput(
     headers: {
       Authorization:         "Bearer " + xagent.serviceJWT(),
       X-Forwarded-Agent-JWT: context.inboundAgentJWT,
       traceparent:           context.traceparent,
       X-Request-ID:          context.requestId
     },
     body: {
       text:       context.final_answer,
       // Phase 4 post-edit: pass the original input_text so output-pii-email-v1 can
       // distinguish "email NOT in input" from "user echoed their own email". xAgent
       // already has it buffered for the PRE-GUARDRAIL call. Omitting it degrades
       // the rule to redact-any-email (over-redaction, which is the safe failure mode).
       input_text: task.input.message,
       task_id:    task.task_id
     }
   )
   if r.decision == "block":  context.error = GUARDRAIL_VIOLATION; return
   if r.decision == "redact": context.final_answer = r.processed_text

📋 MEMORY_WRITE                             (async — fire and forget)
   if shouldStoreMemory(task, context):
     // Direct HTTP — there is NO Kafka memory-write topic. (The previously named
     // cypherx.memory.write.requested is DELETED: Phase 6 never defined or consumed
     // it; writes would have vanished into a void.) The fire-and-forget POST reuses
     // Memory's server-side validation + Idempotency-Key replay.
     async memory.store(                                   // POST /v1/memories
       headers: {
         Authorization:         "Bearer " + xagent.serviceJWT(),
         X-Forwarded-Agent-JWT: context.inboundAgentJWT,
         traceparent:           context.traceparent,
         Idempotency-Key:       deriveIdempotencyKey(task_id, "memory_write")
       },
       body: { content, memory_type, scope, scope_id, ... }
     )   // failures: log + metric only — never fail the task on a memory write

⚡ EVENT  (runs always — completed path AND error path; pipeline runner treats EVENT as a `finally`-equivalent stage)
   BEGIN;
     UPDATE xagent.tasks SET status = $finalStatus, output = ..., tokens_used = ...,
            cost_usd = ..., completed_at = NOW(), error_code = ..., error_msg = ...
       WHERE task_id = $1;
     INSERT INTO xagent.outbox (topic, partition_key, payload) VALUES
       (
         CASE $finalStatus
           WHEN 'completed' THEN 'cypherx.agent.task.completed'
           ELSE                  'cypherx.agent.task.failed'
         END,
         tenant_id::text,
         <Contract 5 envelope JSON>
       );
   COMMIT;
   // The publisher loop reads xagent.outbox and produces to Kafka with the partition_key.
   // Both completed AND failed are first-cycle events per Contract 5 post-edit.

⚡ RETURN context.final_answer   // or context.error
```

**The execution engine struct/class MUST be implemented as a pipeline runner with named stages**, not a procedural function. This is the single most important design constraint: a procedural implementation will be rewritten when Tools/Memory/Skills are added; a pipeline implementation will simply gain new stages.

---

### Component 3b — Transactional Outbox ⚡

Required so `xagent.tasks` UPDATE and `cypherx.agent.task.completed`/`task.failed` Kafka events can never diverge. Same template as Phases 3/4/5/6.

```sql
CREATE TABLE xagent.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,        -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,        -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX idx_outbox_unpublished
  ON xagent.outbox(created_at) WHERE published_at IS NULL;
-- Platform-internal table — no RLS (only xagent-service writes/reads).
```

Publisher loop: one goroutine per pod, batch SELECT 100, publish with `partition_key=tenant_id`, mark `published_at`, exponential backoff on failure, DLQ to `<topic>.dlq` after 10 attempts. Nightly job deletes `outbox` rows where `published_at < NOW() - INTERVAL '7 days'`.

> **Every terminal-status writer uses this outbox — including the timeout sweeper.** The sweeper (Component 2) writes its `status='timeout'` UPDATE and the `cypherx.agent.task.failed` outbox INSERT in one transaction, exactly like the EVENT stage. No code path may mark a task terminal without the matching outbox row.

**Kafka event payloads (matches Contract 5 first-cycle spec):**

```json
Topic: cypherx.agent.task.completed
Partition key: tenant_id
Payload:
{
  "task_id":     "<uuid>",
  "agent_id":    "<uuid>",
  "tenant_id":   "<uuid>",
  "status":      "completed",
  "tokens_used": 1450,
  "cost_usd":    0.00726,
  "duration_ms": 2340,                ← renamed from latency_ms
  "trace_id":    "<uuid>"
}

Topic: cypherx.agent.task.failed
Payload:
{
  "task_id":    "<uuid>",
  "agent_id":   "<uuid>",
  "tenant_id":  "<uuid>",
  "error_code": "GUARDRAIL_VIOLATION", ← or TIMEOUT, BUDGET_EXCEEDED, LLM_ERROR, ...
  "error_msg":  "Input blocked by prompt-injection-v1",
  "trace_id":   "<uuid>"
}
```

---

### Component 4 — MCP Client 📋

> **Relabeled 📋 (was ⚡) — see Amendment Log.** Every consumer of this client (TOOL_LOOP, Tool Registry, tool MCP servers) is Phase 7 / enterprise wave; the client lands together with TOOL_LOOP in WP12. The design below stands as written.

**What it is:** The built-in MCP client that agents use to invoke tools.

```
McpClient interface:
  invoke(toolEndpoint, headers HttpHeaders, body McpInvokeBody) → McpInvokeResponse

Implementation:
  - HTTP client with connection pooling (one pool per tool endpoint).
  - Timeout: 30s default (configurable per tool from manifest).
  - Retry: 3 attempts on connection failure or 5xx (NEVER on 4xx).
  - Circuit breaker scoped per (tool_endpoint, agent_id) — failures of one tool for
    one agent never trip the breaker for another tool or another agent. Opens after 5
    consecutive failures, reset after 60s. State held in-pod (lost on restart, fine).
  - Auth: Authorization = xAgent service JWT; X-Forwarded-Agent-JWT = inbound agent JWT.
    Body carries NO identity fields (Phase 7 standard).
  - Trace: propagate W3C TraceContext (traceparent + tracestate) on every call.
  - Idempotency-Key: derived deterministically from (task_id, tool_call_id) when the
    tool manifest declares idempotent=true.
```

---

### Component 5 — Streaming (SSE) 📋

> **Demoted to 📋 (was ⚡).** Streaming depends on `task.mode = "stream"`, which is itself 📋 in first cycle. The checklist already placed SSE in 📋; this header now matches.

**What it is:** Real-time token streaming from xAgent to the client. Lands when `mode=stream` lands.

```
GET /v1/tasks/{task_id}/stream
  Content-Type: text/event-stream

First-cycle-equivalent events (lands together with mode=stream):
  data: { "type": "status",  "status": "running" }
  data: { "type": "token",   "content": "AI research" }
  data: { "type": "token",   "content": " in 2026" }
  data: { "type": "done",    "status": "completed", "tokens_used": 1450, "cost_usd": 0.00726 }

Tool-event types — gated on Phase 7 TOOL_LOOP being enabled in the agent's pipeline:
  data: { "type": "tool",    "tool": "tool-web-search.web_search", "status": "invoking" }
  data: { "type": "tool",    "tool": "tool-web-search.web_search", "status": "complete", "result_preview": "..." }

On error:
  data: { "type": "error",   "code": "GUARDRAIL_VIOLATION", "message": "..." }
```

**Implementation (when landed):** Task execution writes SSE events to Valkey pub/sub channel (`sse:{task_id}`). `/stream` endpoint subscribes to channel and forwards events to client. Valkey outage → SSE unavailable but sync mode still works (graceful degradation).

---

### Component 6 — Execution Trace & Audit Log ⚡

**PostgreSQL (`xagent.task_steps`):**
```sql
CREATE TABLE xagent.task_steps (
  step_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID         NOT NULL,           -- no FK; RLS-context-friendly app-level link
  tenant_id    UUID         NOT NULL,
  step_type    VARCHAR(30)  NOT NULL,
                            -- guardrail_check | memory_retrieve | llm_call | tool_call |
                            -- memory_write   | skill_load
  step_name    VARCHAR(100) NOT NULL,
                            -- discriminator matching Contract 15 + Contract 3 task_steps entries:
                            --   'guardrail_check_input', 'guardrail_check_output',
                            --   'llm_call', 'tool_call:<server>.<fn>',
                            --   'memory_retrieve', 'memory_write', 'skill_load'
  status       VARCHAR(20)  NOT NULL,           -- running | passed | failed | timeout | redacted
  input        JSONB,
  output       JSONB,
  duration_ms  INTEGER,                          -- renamed from latency_ms (Contract 5 parity)
  tokens_used  INTEGER,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,

  CONSTRAINT step_type_enum CHECK (step_type IN ('guardrail_check','memory_retrieve','llm_call','tool_call','memory_write','skill_load')),
  CONSTRAINT status_enum    CHECK (status IN ('running','passed','failed','timeout','redacted'))
);

CREATE INDEX idx_steps_task_id ON xagent.task_steps(task_id);

ALTER TABLE xagent.task_steps ENABLE ROW LEVEL SECURITY;
CREATE POLICY xagent_task_steps_isolation ON xagent.task_steps FOR ALL
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

**Per-stage write rule (MUST enforce — Contract 15 smoke test depends on it):**

The execution engine writes EXACTLY one `task_steps` row per pipeline stage at stage completion. First-cycle pipeline (LOAD/PROMPT_BUILD don't write rows; the 3 user-visible stages do):

| Pipeline stage | step_type | step_name | status mapping (guardrails decision → row status) |
|---------------|-----------|-----------|---------------------------------------------------|
| PRE-GUARDRAIL  | `guardrail_check` | `guardrail_check_input`  | allow|warn|redact → `passed`; block → `failed` |
| LLM            | `llm_call`        | `llm_call`               | success → `passed`; provider error → `failed`; timeout → `timeout` |
| POST-GUARDRAIL | `guardrail_check` | `guardrail_check_output` | (same mapping as PRE) |

> **Status mapping codified (2026-06):** a `redact` decision maps to `passed` — the pipeline continued with the processed text, so the stage succeeded; the redaction itself is recorded in the step row's `output` payload (`decision: "redact"`). The `redacted` enum value stays in the CHECK constraint (reserved; unused by the first-cycle mapping) to avoid schema churn.

Contract 15 test #7 asserts exactly these three rows (in order) for a successful first-cycle task. The query `WHERE task_id = $1 ORDER BY created_at` returns them; smoke test compares `step_name IN ('guardrail_check_input','llm_call','guardrail_check_output')` and status mapping.

When TOOL_LOOP and MEMORY stages land (📋), additional rows appear with `step_name='tool_call:<server>.<fn>'` etc.

`task_steps` also feeds the A2A response `task_steps` field in Contract 3 — the response JSON is built directly from this table. `duration_ms` field aligns the two.

---

### Component 7 — Agent Capability Advertisement ⚡

```
GET /v1/capabilities
Response:
{
  "agent_id":    "<uuid>",
  "name":        "Research Agent",
  "version":     "1.0.0",
  "capabilities": [
    { "type": "research",    "description": "Web research and summarisation" },
    { "type": "summarise",   "description": "Document summarisation" }
  ],
  "tools":       ["tool-web-search"],
  "skills":      ["research-and-summarise"]
}
```

---

## Sub-Phase 9B — A2A Communication 📋

### Component 8 — A2A Task Receiver 📋

**What it is:** Every agent exposes an A2A endpoint to receive tasks from other agents.

```
POST /v1/a2a/tasks
  Auth: Bearer <sender-agent-a2a-jwt>
  Body: A2A message schema (Contract 3 from Phase 0)

The agent:
  1. Validates the A2A JWT (signed by Auth service, delegation scope)
  2. Extracts task from A2A message
  3. Executes task (same execution engine as Component 3)
  4. Returns result sync OR posts to callback_url async
```

---

### Component 9 — A2A Task Sender 📋

**What it is:** xAgent can send tasks to other agents via A2A.

```
Internally called when execution engine identifies a sub-task
to delegate to a specialist agent:

1. Query Agent Registry: which agent handles this task type?
2. Request A2A delegation token from Auth service
3. POST to target agent's /v1/a2a/tasks endpoint
4. Wait for sync response OR poll via GET /v1/tasks/{task_id}
5. Use result in current task execution
```

---

### Component 10 — Agent Registry (for A2A Discovery) 📋

**PostgreSQL (`xagent.agent_registry`):**
```sql
CREATE TABLE xagent.agent_registry (
  agent_id       UUID PRIMARY KEY,
  tenant_id      UUID NOT NULL,
  name           VARCHAR(255) NOT NULL,
  a2a_endpoint   VARCHAR(500) NOT NULL,
  capabilities   JSONB NOT NULL,       -- what task types this agent handles
  status         VARCHAR(20) DEFAULT 'active',
  last_heartbeat TIMESTAMPTZ
);
```

**Agent-to-agent discovery:**
```
GET /v1/registry/agents?capability=research
→ Returns list of agents that can handle research tasks
```

---

## Sub-Phase 9C — Orchestration Engine 📋

### Component 11 — Orchestrator Agent 📋

**What it is:** A special meta-agent that receives high-level goals, decomposes them into subtasks, routes subtasks to specialist agents, and synthesises results.

```
POST /v1/workflows
Body:
{
  "goal": "Research quantum computing trends and write a technical report",
  "context": {},
  "agents": ["<optional: specify which agents to use>"],
  "timeout_seconds": 300
}
```

**Decomposition flow:**
```
1. LLM call: "Decompose this goal into subtasks with agent routing hints"
   → { subtasks: [
        { id: "t1", description: "Research quantum computing", type: "research" },
        { id: "t2", description: "Write technical report", type: "content-generation", depends_on: ["t1"] }
      ] }

2. Build dependency graph (DAG of subtasks)

3. Execute DAG:
   - Find subtasks with no dependencies → execute in parallel
   - When dependency completes → unlock dependents → execute
   - Collect all results

4. Synthesis LLM call:
   "Given these research findings (...), write the final technical report"

5. Return final output
```

---

### Component 12 — Workflow State Machine 📋

**PostgreSQL (`xagent.workflows` + `xagent.workflow_tasks`):**
```sql
CREATE TABLE xagent.workflows (
  workflow_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID NOT NULL,
  goal         TEXT NOT NULL,
  status       VARCHAR(20) DEFAULT 'pending',
      -- pending | planning | running | completed | failed | cancelled | awaiting_approval
  subtask_dag  JSONB,           -- serialised dependency graph
  output       JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE TABLE xagent.workflow_tasks (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id   UUID NOT NULL REFERENCES xagent.workflows(workflow_id),
  task_id       UUID,           -- xagent.tasks reference once created
  description   TEXT NOT NULL,
  task_type     VARCHAR(100),
  assigned_agent_id UUID,
  depends_on    UUID[],         -- IDs of sibling workflow_tasks
  status        VARCHAR(20) DEFAULT 'pending',
  output        JSONB
);
```

---

### Component 13 — Human-in-the-Loop Checkpoint 📋

```
Workflow can pause at defined checkpoints:
  POST /v1/workflows/{workflow_id}/approve
  Body: { "approved": true, "notes": "Looks good, proceed" }

Checkpoint types:
  - Before irreversible actions (sending emails, making API calls)
  - Before large LLM calls (cost threshold)
  - When output confidence is low

Status while waiting: "awaiting_approval"
Timeout: workflow cancelled if not approved within configured window
```

---

### K8s Deployment Spec

```yaml
Namespace:   xagent
Deployments:
  agent-runtime:          (9A) min 2, max 20 replicas, c5.2xlarge nodes — ⚡
  orchestrator:           (9C) min 2, max 10 replicas, c5.2xlarge nodes — 📋
  a2a-router:             (9B) min 2, max 8 replicas, c5.large nodes    — 📋

Resources (agent-runtime):
  requests: { cpu: 500m, memory: 768Mi }
  limits:   { cpu: 2000m, memory: 2Gi }

Startup probe (pipeline init + JWKS warm-up takes ~5–10s on cold start):
  startupProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    failureThreshold: 12          # 60s grace

Health probes (Contract 7):
  livenessProbe:
    httpGet: { path: /livez, port: 8080 }
    periodSeconds: 10
    # Process-only — NEVER touches DB / downstream services.
  readinessProbe:
    httpGet: { path: /readyz, port: 8080 }
    periodSeconds: 5
    # Hard deps (fail readiness) — Postgres + Auth JWKS ONLY (2026-06 decision;
    # blesses the shipped code's divergence — gating on downstream /livez caused
    # readiness flaps for failures the request path already surfaces per-call):
    #   - PostgreSQL reachable
    #   - Auth JWKS resolved + cached
    # Soft deps (log + metric only, do NOT fail readiness):
    #   - LLMs Gateway / Guardrails (request-path errors surface as per-call 5xx + retries)
    #   - Valkey (agent cache; missing → DB lookup per task, slower not broken.
    #     NOTE: POST /v1/tasks idempotency fails CLOSED with 503 on Valkey outage —
    #     a deliberate request-path rule, not a readiness gate)
    #   - Kafka (outbox keeps events durable until publisher reconnects)
    #   - Memory / RAG / Tool Registry (enhanced-pass dependencies)

Env vars (from Doppler):
  DATABASE_URL                  (PgBouncer → xagent schema, runtime user xagent_user)
  VALKEY_URL                    (cache + cancellation signals + future SSE pub/sub)
  KAFKA_BROKERS
  KAFKA_SASL_PASSWORD
  AUTH_SERVICE_URL              (http://auth-service.shared-core.svc.cluster.local:8080)
  AUTH_JWKS_URL                 (http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json)
  SERVICE_BOOTSTRAP_SECRET      (Contract 12; from service-auth/xagent/bootstrap_secret)
  LLMS_GATEWAY_URL              (http://llms-gateway.shared-core.svc.cluster.local:8080)
  GUARDRAILS_SERVICE_URL        (http://guardrails-service.shared-core.svc.cluster.local:8080)
  MEMORY_SERVICE_URL            (http://memory-service.shared-core.svc.cluster.local:8080)
  RAG_SERVICE_URL               (http://rag-service.shared-core.svc.cluster.local:8080)
  TOOL_REGISTRY_URL             (http://tool-registry.tools.svc.cluster.local:8080)
```

> **Service ACL coverage** (already provisioned across prior phases; listed here for clarity):
>
> First cycle (Phase 2 + Phase 4 seed migrations cover these):
> - `xagent → auth-service       [internal:read]`
> - `xagent → llms-gateway       [internal:read, internal:write]`
> - `xagent → guardrails-service [internal:read, internal:write]`
>
> Enhanced (added by the respective phase migrations as they land):
> - Phase 5: `xagent → rag-service [internal:read]`
> - Phase 6: `xagent → memory-service [internal:read, internal:write]`
> - Phase 7: `xagent → tool-registry [internal:read]` + `xagent → tool-* [internal:write]` per tool
> - Phase 8: `xagent → tool-skill-retriever [internal:write]`
>
> No new ACL rows are added in Phase 9 itself — the receiving phases ship them.

> **JWKS verification** follows the Phase 3 standard: in-cluster URL primary (5-min cache, refresh-on-`kid`-miss rate-limited to 1/min). xAgent verifies BOTH the inbound agent JWT (re-verify, defense in depth) AND every JWT received via `X-Forwarded-Agent-JWT` on A2A inbound (📋).
>
> **External-receiver JWKS path:** For A2A federation across customer clusters (external runtimes participating in delegation chains — Phase 10 Component 0), JWKS MUST be reachable at the public `{AUTH_ISSUER_URL}/.well-known/jwks.json` endpoint AND via the signed bundle `{AUTH_ISSUER_URL}/.well-known/jwks-signed.json` (Phase 2 Component 3). xAgent inside our cluster uses the cluster-DNS path; external A2A receivers use the public path. The previous "never via ALB" guidance applies only to **internal** services to avoid latency on hot-path internal calls — it MUST NOT block external receivers from reaching the public JWKS endpoint over HTTPS PKI + signed-bundle verification.

---

## ⚡ First Cycle Implementation Checklist (Sub-Phase 9A — simplified path, no tools/memory/skills)

- [ ] Agent runtime service architecture planned separately
- [ ] Agent runtime config endpoints `POST`/`GET`/`PUT /v1/agents/{agent_id}/runtime` — POST validates against Auth `GET /v1/agents/{id}` at insert (create-only, idempotent, never overwrites); PUT updates runtime fields, **bumps `runtime_version`**, and drives status transitions (`active ↔ inactive`, `pending_config → active`); tasks against unconfigured agents → 409 CONFLICT
- [ ] Agent definition schema + PostgreSQL table (`xagent.agents`) with **CHECK enums** (`memory_scope`, `status`, `temperature`); `memory_scope` enum uses `tenant` (not `global` — Phase 6 parity); `allowed_tools` entries are version-pinned (`tool-X@1.0.0`)
- [ ] **RLS** on `xagent.agents`, `xagent.tasks`, `xagent.task_steps` (Contract 13)
- [ ] Atlas migrations (Contract 14)
- [ ] Task submission endpoint (`POST /v1/tasks`) — **sync mode only** for first cycle; `mode ∈ {async, stream}` → 422 `VALIDATION_ERROR`
- [ ] **Caller-vs-target rule**: `body.agent_id == jwt.agent_id` enforced before any load/persistence; mismatch → 422 `VALIDATION_ERROR`; cross-agent invocation only via 9B A2A delegation tokens (📋)
- [ ] **Idempotency-Key on `POST /v1/tasks` (Contract 9)** — Valkey idem key, 24h TTL; replay returns the original response with the `Idempotent-Replayed` header; same key + different body → 409 `IDEMPOTENCY_KEY_CONFLICT`; **fail-closed 503 on Valkey outage**
- [ ] **`tasks.input` server-side size cap at 256 KiB** (Contract 3 A2A parity)
- [ ] **`xagent.tasks.session_id`** (optional Contract-3 `input.session_id`) + **`xagent.tasks.metadata`** JSONB columns in the schema (MEMORY-stage consumption of `session_id` is 📋)
- [ ] **`timeout_seconds` clamped to [1, 900]** (Contract 3 A2A parity)
- [ ] **Per-task timeout enforcement**: in-pod `context.WithTimeout` + 30s in-process background sweeper loop (cloud form: K8s CronJob) for orphaned `running` rows — sweeper writes `status='timeout'` + `task.failed` outbox row in ONE transaction
- [ ] Task status/result endpoint (`GET /v1/tasks/{task_id}`)
- [ ] Cooperative task cancellation (`DELETE /v1/tasks/{task_id}`) — Valkey `cancellation_signals:{task_id}` flag polled between stages; semantics: 202 pending/running, 409 terminal, 404 under RLS, 503 Valkey down; submitting agent or `agent:admin` only
- [ ] **Pipeline-based execution engine** (Component 3) with first-cycle stages: LOAD → PRE-GUARDRAIL → PROMPT_BUILD → LLM (single round-trip, no tools) → POST-GUARDRAIL → EVENT → RETURN. EVENT runs in a `finally`-equivalent (always fires).
- [ ] **JWT forwarding rule** implemented — inbound agent JWT captured + forwarded verbatim via `X-Forwarded-Agent-JWT` to every downstream; xAgent's own service JWT (Contract 12) in `Authorization`; `traceparent` propagated
- [ ] **NO identity in downstream call bodies** — all downstream calls (Guardrails, LLMs, Tools) carry identity in headers only (Contract 13 anti-pattern guard)
- [ ] **Local JWKS verify** on inbound JWT (defense in depth); layer-B stateful checks (suspended tenant, plan tier, budget hard-stop) via **Valkey-cached Auth `/v1/authorize` verdict on task submission** — key `(tenant_id, agent_id)`, action `task:execute`, 60s TTL; fail-closed on deny, fail-open + alert on Auth outage
- [ ] Service-JWT (Contract 12) for outbound calls to Auth/Guardrails/LLMs via `SERVICE_BOOTSTRAP_SECRET`
- [ ] Guardrails: PRE-LLM `/check/input` integration — service-JWT + X-Forwarded-Agent-JWT
- [ ] Guardrails: POST-LLM `/check/output` integration — passes `input_text` (Phase 4 post-edit) so `output-pii-email-v1` can distinguish echo vs leak
- [ ] LLMs Gateway: chat completions integration (non-streaming); `max_tokens = min(agent.max_tokens, agent.token_budget_per_task)` enforces first-cycle budget on the single call (multi-call budget enforcement is 📋 with TOOL_LOOP)
- [ ] **Tool-use loop is NOT first cycle** — Phase 7 not yet built. Stage `TOOL_LOOP` exists in pipeline definition but is disabled.
- [ ] **Memory integration is NOT first cycle** — Phase 6 not yet built.
- [ ] **Skill retrieval is NOT first cycle** — Phase 8 not yet built.
- [ ] Task step audit log (`xagent.task_steps`) — **exactly 3 rows per first-cycle task** (`guardrail_check_input`, `llm_call`, `guardrail_check_output`); status mapping per Component 6 table; `duration_ms` field (not `latency_ms`)
- [ ] **Outbox** (`xagent.outbox`) — tasks UPDATE + outbox INSERT in one transaction; publisher loop with DLQ after 10 attempts
- [ ] **Kafka events** `cypherx.agent.task.completed` AND `cypherx.agent.task.failed` (both ⚡ per Contract 5 post-edit); payload includes `status` + `duration_ms` + Contract 5 envelope; partition key = `tenant_id`
- [ ] Capabilities endpoint (`GET /v1/capabilities`)
- [ ] **`/livez`, `/readyz`, `/metrics`** endpoints; readiness gated on **Postgres + Auth JWKS only**; LLMs/Guardrails/Valkey/Kafka/Memory/RAG/Tools are soft deps
- [ ] **Startup grace** configured — compose healthcheck `start_period: 60s` (cloud form: K8s startup probe, 60s grace)
- [ ] Container resource sizing documented for the compose runtime (cloud form: K8s 500m/768Mi req, 2000m/2Gi lim — memory bumped from 512Mi)
- [ ] `AUTH_JWKS_URL` + `SERVICE_BOOTSTRAP_SECRET` env vars; JWKS 5-min cache + refresh-on-`kid`-miss-1/min
- [ ] MCP client (Component 4) — (moved to 📋 — see Amendment Log; lands with TOOL_LOOP in WP12)
- [ ] Runs in the local compose stack (xagent service container + healthchecks; cloud form: K8s `xagent` namespace via ArgoCD)
- [ ] **Passes Contract-15 cases 1–10 (First-Cycle Smoke Test)** end-to-end, twice, on a freshly deployed dev environment — including test #7 which asserts `task_steps = [guardrail_check_input, llm_call, guardrail_check_output]`. Cases 11–15 gate the enterprise wave (WP12/WP14), not 9A.

## 📋 Full Enterprise Implementation Checklist

- [ ] SSE streaming endpoint (`GET /v1/tasks/{task_id}/stream`) — Valkey pub/sub bridge per task; tool events gated on TOOL_LOOP
- [ ] Async task mode (`mode=async` fire-and-poll)
- [ ] Enable `TOOL_LOOP` stage in pipeline; wire MCP client to Tool Registry (requires Phase 7)
- [ ] **MCP client (Component 4)** — circuit breaker scoped per (tool_endpoint, agent_id); retry on connection/5xx only, never on 4xx; `Idempotency-Key` derived from `(task_id, tool_call_id)` for idempotent tools (lands with TOOL_LOOP — WP12; relabeled from ⚡, see Amendment Log)
- [ ] Enable `MEMORY_RETRIEVE` and `MEMORY_WRITE` stages (requires Phase 6) — MEMORY_WRITE posts directly to Memory `POST /v1/memories` (NO Kafka write-request topic); sessions registered idempotently via `POST /v1/memories/sessions` before first session-scope use (`tasks.session_id` producer)
- [ ] **Enable `RAG_QUERY` stage** — per-agent `allowed_kb_ids`, `rag_top_k_per_kb`, `rag_min_score` columns wired; iterates KBs, merges chunks into system prompt context, KB ACL enforced server-side by RAG (requires Phase 5)
- [ ] Enable `SKILL_LOAD` stage; execute skill steps as a sub-pipeline (requires Phase 8); resolves `required_capabilities` per Phase 7 Component 1c
- [ ] Multi-call token budget enforcement (BUDGET_EXCEEDED check inside TOOL_LOOP)
- [ ] Task listing with pagination and filters
- [ ] **Kafka consumer for `cypherx.auth.agent.registered`** — auto-create stub `xagent.agents` row with status `pending_config` so owners only need to fill the system prompt
- [ ] **Sub-phase 9B**: A2A receiver endpoint + sender client
- [ ] **Sub-phase 9B**: Agent registry for A2A discovery
- [ ] **Sub-phase 9B**: A2A JWT delegation token usage
- [ ] **Sub-phase 9B**: Retry + circuit breaker for A2A calls
- [ ] **Sub-phase 9C**: Orchestrator agent type
- [ ] **Sub-phase 9C**: Goal decomposition (LLM-powered)
- [ ] **Sub-phase 9C**: Workflow DAG execution (sequential + parallel)
- [ ] **Sub-phase 9C**: Human-in-the-loop approval checkpoint
- [ ] **Sub-phase 9C**: Workflow status and graph API
- [ ] Distributed trace: trace_id propagated through all sub-calls (already required by Contract 8 — verify e2e)
- [ ] Execution timeline: per-step timing dashboard from `xagent.task_steps`
- [ ] Cost per task dashboard (from `xagent.tasks.cost_usd`)
- [ ] Guardrails circuit breaker — when xAgent trips on N consecutive guardrails 5xx, fail-open for non-block rules and emit `cypherx.guardrails.breaker.tripped` (Phase 4 post-edit's deferred companion)
- [ ] **Passes Contract-15 cases 11–15** — enterprise-wave gate (WP12/WP14); includes the Idempotency-Key cases (12/13) exercised against the ⚡ implementation

---

## Audit Addenda — Post-Design Risk Review (2026-05-25)

### 1. Prompt Context Explosion — REAL
Evidence: lines 332–372 (MEMORY/RAG/SKILL concatenated without budget).
**Mitigation:** `memory_context + rag_context + skill_instructions` combined ≤30 % of `agent.token_budget_per_task`; truncate RAG (FIFO) → memory → skills; emit `context_truncated` in `task_steps`.

### 2. Tool Loop Iteration Limits — REAL
Evidence: lines 399–423 (token cap only).
**Mitigation:** `max_iterations = 10` per task; exceed → `TOOL_LOOP_EXCEEDED`; audit row `step_name='tool_loop_limit'`.

### 3. Orchestrator Cost Scaling — REAL
Evidence: lines 152, 385, 421 (no cost ceiling).
**Mitigation:** add `cost_budget_per_task NUMERIC(12,8) NOT NULL DEFAULT 1.00` (USD) to `xagent.agents`; task fails on exceed.

### 4. PostgreSQL Runtime Config Choice — PARTIAL
Evidence: lines 115–172.
**Mitigation:** justification — agent config read on every task load (line 317) with Valkey 5 min TTL; low write frequency; PG + cache sufficient. Read-replica/CDC if >1000 agents/tenant.

### 5. Authentication Model — REAL
Evidence: lines 97–98 (forwarding rule stated; xAgent's own identity unclear).
**Mitigation:** xAgent authenticates to downstream services as the xAgent service principal (Contract 12 service JWT). Inbound agent JWT forwarded verbatim as `X-Forwarded-Agent-JWT`. xAgent never calls Auth `/authorize` per-task; only for stateful checks (suspended tenant, plan tier, budget hard-stop).

### 6. Transactional Outbox Pattern — REAL (atomicity claim needs strengthening)
Evidence: lines 475–495.
**Mitigation:** `UPDATE xagent.tasks` + `INSERT xagent.outbox` MUST be a single transaction. Publisher polls outbox, sets `published_at` only on Kafka ACK. On publish failure: retry with exponential backoff; DLQ after 10 attempts.

### 7. Phase Separation Strategy — REAL
Evidence: lines 9, 86–87, 293, 361–362, 896–898 (refs to phases 5/6/7/8 marked 📋).
**Mitigation:** enhancement stages MUST default `enabled=false` in first-cycle agent config; explicit feature-flag per stage to avoid runtime errors when downstream phases slip.

### 8. Missing Centralized Policy Engine — REAL
Evidence: lines 405–406 (inline tool authz).
**Mitigation:** static `agent.allowed_tools` acceptable first cycle. For Phase 9B multi-agent, externalize to policy service (OPA-compatible).

### 9. A2A Federation Complexity — REAL
Evidence: lines 653–709.
**Mitigation:** A2A receiver verifies delegation signature (JWKS, org-issuer). Reject if calling `agent_id` appears earlier in chain (loop detection). Outbound A2A uses request-scoped delegation token (Auth `/delegate`) scoped to (target_agent_id, task_type, tenant_id) — not reusable JWT.

### 10. Task Step Audit Architecture — PARTIAL
Evidence: lines 582–629.
**Mitigation:** worst-case ~16 rows/task (10-iter TOOL_LOOP + 2 memory ops + 1 skill). Retention: `DELETE FROM xagent.task_steps WHERE completed_at < NOW() - INTERVAL '90 days'` to bound table growth.

### 11–12. Senior-Level Architectural Decisions / Final Verdict — NOT-REAL
No such sections exist in the doc; these appear to be reviewer-summary headers from a separate audit pass, not concerns about doc gaps.
