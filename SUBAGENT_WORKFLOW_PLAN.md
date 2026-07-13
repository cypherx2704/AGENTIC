# CypherX Sub-Agent Orchestration — Implementation Plan

> ## ⚠️ PARTIALLY SUPERSEDED — 2026-07-13
>
> **This plan's decomposition design was reversed during implementation.** It specifies
> *"deterministic templates first, LLM decomposition only when needed"* — a keyword router
> (`match_template()`) that mapped goal text to a fixed `researcher → writer → reviewer` shape.
> **That is now deleted, and re-introducing it is forbidden.** It was a routing rule in disguise: it
> chose the sub-agents itself, and being a substring matcher it could not read a negation — *"…and do
> NOT write a brief"* matched the `write` keyword and produced a brief-writing step anyway.
>
> **The rule that replaced it:** *the orchestrator's LLM planner decides the steps and which agent runs
> each one. The backend only validates* — acyclic, within the depth/fanout caps, and every named target
> a real roster entry. It never chooses, invents, or substitutes a target. An invalid plan goes back to
> the planner once (gated by a HIL approval); failing that, the run fails `ORCHESTRATION_FAILED`. The
> only non-planner graph is `solo`: one node run by the orchestrator itself = no delegation.
>
> **Everything else in this plan shipped as written** — the internal (no-A2A) sub-agent execution path,
> summary-only returns, the live SSE execution tree, the budget ceiling, and HIL gating. Sections that
> are stale are marked ~~struck through~~ inline below.
>
> **Current state of record:** [SUBAGENT_WORKFLOW_EXPLAINED.md](SUBAGENT_WORKFLOW_EXPLAINED.md) and
> [orchestration/decompose.py](xAgent/ax-1/src/agent_runtime/orchestration/decompose.py).
> This document is kept for the *reasoning* behind the decisions, not as the design of record.

> **Status:** DRAFT for founder review · **Branch:** `service-enhancement` · **Author:** planning pass (recon-verified against live code, 2026-07-12)
>
> Goal: build the full **PROMPT → ORCHESTRATOR → SUB-AGENTS** workflow "just like Claude" — an orchestrator that decomposes a prompt, spawns scoped sub-agents, maintains a live execution tree, and synthesizes a result — **using a lightweight internal path (no A2A internally)** and reusing the governance layer that already exists. Reviewed and approved *before* implementation begins.

---

## 0. TL;DR + the one decision to confirm

**What already works (REAL, verified in code):** the *governance* layer. One orchestrator per tenant; only it can create/manage sub-agents; sub-agent scopes are a create-time subset of the orchestrator's; hierarchy rides the agent's **own JWT** (`agent_type`, `parent_orchestrator_id`) — *not* a delegation chain; sub-agent LLM confinement is enforced at the LLMs gateway; HIL modes + approvals are wired end-to-end. **This already satisfies your "A2A only where required" rule.**

**What's missing (the whole point of this plan):** the *execution* engine. Nothing decomposes a goal, spawns sub-agent runs, tracks a tree, enforces a budget, or synthesizes results. `xAgent/ax-2` (the spec's orchestrator home) is an empty repo; ax-1 runs exactly one agent and forbids cross-agent submission.

**The plan in one line:** add an **orchestration engine** that reuses ax-1's existing single-agent pipeline to run each sub-agent node internally (in-tenant, `on_behalf_of` the sub-agent — no A2A, no per-child token mint), driven by a DAG built from ~~**deterministic templates first, LLM decomposition only when needed**~~ **the orchestrator's LLM planner — which alone decides the steps and which agent runs each one; the backend only validates the result and never substitutes a target** *(reversed 2026-07-13 — see the banner)*, with a **live SSE execution tree**, a **per-workflow budget ceiling**, and **HIL gating** on sub-agent creation / risky tools. Reserve the heavy A2A router + delegation chains strictly for the future external/cross-vendor boundary.

### ⚠️ Decision to confirm before coding (§11 has the full trade-off)
**Where does the orchestration engine live?**
- **Recommended (MVP): inside `xAgent/ax-1`** as a *new module + endpoint* (`POST /v1/orchestrations`, not a pipeline stage). Reuses ax-1's DB pool, auth, outbox, Valkey, downstream clients, SSE, and sweeper → lowest complexity, no new service. Clean seam to extract later.
- **Alternative: a new `xAgent/ax-2` service** — the spec's intended home; cleaner separation and independent scaling, but a whole new service (Dockerfile, compose, migrations job, BFF upstream, CI, deploy). More cost/complexity now.

Everything below is written to work for **either** host; the only thing that changes is the package location. The plan assumes the recommended ax-1 host unless you choose otherwise.

---

## 1. Current state — the honest map (REAL / STUB / MISSING)

### 1.1 Governance layer — Auth service (Kotlin) — **REAL & wired**
| Capability | Status | Where |
|---|---|---|
| One orchestrator per tenant (physical unique index) | REAL | `auth` `20260623_0010__init.sql` `uq_orchestrator_per_tenant` |
| `agent_type ∈ {orchestrator, sub_agent, user_created}`, `parent_orchestrator_id`, `owner_user_id` | REAL | `auth.agents` + `domain/Enums.kt` |
| Orchestrator-only sub-agent CRUD `/v1/orchestrator/sub-agents` (POST/GET/PATCH/DELETE) | REAL | `OrchestratorController/Service.kt` |
| Depth-1 cap (`SUB_AGENT_CANNOT_DELEGATE`) | REAL | `OrchestratorService.kt` |
| Sub-agent scopes ⊆ orchestrator (create/update) → 422 w/ `exceeding` | REAL | `OrchestratorService.kt` |
| Hierarchy in agent's own JWT (`agent_type`, `parent_orchestrator_id`) | REAL | `TokenMintService.kt`; parsed in `ax-1 core/auth.py` |
| Sub-agent LLM confinement via per-agent alias allowlist | REAL | `llms` `alias_service.enforce_agent_alias`, 403 `LLM_ALIAS_NOT_ALLOWED` |
| HIL modes `automated/human_in_loop/partial` + `orchestrator_hil_config` | REAL | `HilService.kt`, `20260623_0012__init.sql` |
| HIL request/grant/deny/list `/v1/hil/approvals*` | REAL | `HilController.kt` |
| `immutable_llm` column/claim/field | **STUB** | persisted, never set `true`, never enforced |
| `sub_agent_creation` HIL trigger actually blocking creation | **STUB** | trigger is selectable, but `createSubAgent` never calls HIL |
| Contract-16 step-up token / `X-Approval-Token` / `approval_grants` consume | **MISSING** | schema only, no code |
| A2A / delegation-chain issuance (`/v1/agents/{id}/a2a-token`) | **MISSING** | spec-only (Phase 2 Component 8d) |

### 1.2 Execution layer — xAgent — **mostly MISSING**
| Capability | Status | Where |
|---|---|---|
| Single-agent pipeline `LOAD→PRE_GUARDRAIL→PROMPT_BUILD→LLM→POST_GUARDRAIL→EVENT` | REAL | `ax-1 core/pipeline.py` |
| `tool_loop` stage (MCP tool-calling loop, ranking, caps, HIL `ask` gate) | REAL but **flag-OFF** | `ax-1 core/stages/tool_loop.py` |
| Async task + Idempotency-Key, SSE `/stream`, cancel (Valkey+HTTP), sweeper | REAL | `ax-1 api/tasks.py`, `services/sweeper.py` |
| `on_behalf_of=<agent_id>` service tokens forwarded downstream | REAL | `ax-1 services/service_token.py` |
| Cross-agent submission (orchestrator runs a *different* agent) | **BLOCKED** | caller-vs-target rule `body.agent_id == jwt.agent_id` |
| Goal decomposition, DAG, `xagent.workflows`/`workflow_tasks` | **MISSING** | spec-only; not in code |
| Parent/child task linkage (`parent_task_id`), fan-out/join, run tree | **MISSING** | — |
| Kafka cancel consumer, sessions table | **MISSING** | cancel is Valkey+HTTP; sessions live only as `tasks.session_id` |
| `xAgent/ax-2` (orchestrator + a2a-router) | **EMPTY REPO** | — |

### 1.3 Contracts — **A2A single-hop REAL; workflow/DAG MISSING**
- A2A `task-request`/`task-response` **enforced now** (sync/async/stream, 256 KiB caps, SSRF+HMAC callback, idempotency) — but response models **only terminal status** (no streaming-chunk schema). The request schema already **reserves** `workflow_id` + `parent_task_id` extension fields.
- Delegation chain schema exists but **STUB** (`x-enforcement-phase: phase-10`, JWT-borne).
- Approval token (Contract 16) **STUB** (`phase-02`).
- **MISSING:** any workflow DAG / orchestration-run / subtask-tree contract, and a workflow-level Kafka event. `task-types.md` has an advisory `plan` type (`steps[{step, depends_on[]}]`) — the closest thing to a DAG.

### 1.4 Frontend — **read/config REAL; orchestrated-run MISSING**
- REAL: `/orchestrator` (sub-agent CRUD), `/hil` (config + approvals), `/tasks/run` (single-agent submit + SSE pipeline), `AgentBuilder` (full per-agent runtime config), reusable `ScopeSelector` / `AgentToolPicker` / `Pipeline` / `TaskTimeline`.
- MISSING: any "run the orchestrator" action, execution tree/DAG view, sub-agent-attributed step stream, "use sub-agents" toggle + selector, inline HIL during a run. Also: the frontend `Session` type **drops `agent_id`** (server has it) — needed so a page knows "which orchestrator am I".
- BFF: adding a new upstream is **config-only** *except* the SSE relay is **hardcoded to `xagent`** — a new stream route needs a one-line code change (moot if we host in ax-1 and reuse the `xagent` upstream).

---

## 2. Target architecture

### 2.1 The flow
```
                    ┌──────────────────────────────────────────────────────────┐
  PROMPT ──────────►│ ORCHESTRATOR  (the tenant's one orchestrator agent)       │
  (task runner,     │  1. intent + mode: run solo OR use sub-agents             │
   sub-agents ON)   │  2. DECOMPOSE goal → DAG   (LLM planner decides; the      │
                    │     backend only validates. `solo` = no delegation)       │
                    │  3. validate DAG (acyclic) + apply caps (depth/fanout/$)  │
                    │  4. DRIVE the DAG: run in-degree-0 nodes in parallel      │
                    │  5. HIL gate (sub_agent_creation / risky tool) if needed  │
                    │  6. SYNTHESize child summaries → final answer             │
                    └──────────────┬───────────────────────────────────────────┘
                                   │ per node (INTERNAL, no A2A):
                                   │ run ax-1 pipeline for the assigned SUB-AGENT,
                                   │ under on_behalf_of=<sub_agent_id>, RLS tenant-scoped,
                                   │ own budget/timeout/cancel. Returns ONLY a summary.
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
      ┌─────────────┐       ┌─────────────┐       ┌─────────────┐
      │ sub-agent A │       │ sub-agent B │  ...  │ sub-agent N │   each = a scoped agent
      │  (whatever  │       │  (whatever  │       │  (whatever  │   with its OWN runtime:
      │  the tenant │       │  the tenant │       │  the tenant │   description/model/prompt/
      │   named it) │       │   named it) │       │   named it) │   tools/scopes
      └─────────────┘       └─────────────┘       └─────────────┘
        ↑ NO reserved roles. The planner routes on each agent's DESCRIPTION + TOOLS, never its name.
             │  LLM + tools (MCP) + RAG + memory, confined by its scope subset + alias allowlist
             ▼
        summary + citations ──► back up to the orchestrator (NOT the transcript)
```

### 2.2 The internal-vs-A2A boundary (your directive, made concrete)
| Path | Mechanism | Use for |
|---|---|---|
| **Internal orchestration** (this plan) | Orchestrator runs each sub-agent's pipeline **in-process**, RLS tenant-scoped, downstream calls carry `on_behalf_of=<sub_agent_id>`. Confinement enforced downstream (alias allowlist, tool ACLs). **No a2a-router, no chain-walk, no per-child JWT mint, no HMAC callback.** | orchestrator → its own sub-agents, same tenant |
| **A2A** (future, NOT this plan) | `a2a-router` + chain-aware JWT + delegation chain + SSRF/HMAC callbacks (Phase 10 spec) | external company agent ↔ our agent; cross-platform; cross-vendor; cross-tenant federation |

**Why internal is safe without A2A:** the orchestrator is already authenticated; each sub-agent belongs to it (`parent_orchestrator_id`) in the same tenant; the sub-agent's authority is *narrower by construction* (scope subset persisted at create) and re-enforced at every downstream boundary (LLM alias allowlist, tool access grants) via `on_behalf_of`. The chain-walk that A2A does is *redundant* inside one tenant with a one-level hierarchy.

### 2.3 Identity model for a sub-agent run (no new tokens)
- The orchestration driver loads the sub-agent's runtime config (`agents_repo`, RLS) and runs the standard pipeline for it.
- Downstream identity = ax-1's existing service token with `on_behalf_of=<sub_agent_id>` (already how ax-1 forwards identity). LLMs gateway / tools registry / guardrails then apply the sub-agent's own confinement.
- **This sidesteps the known auth gap** (newly created sub-agents get no api_key) — we never need to mint a per-child agent JWT for the internal path.

---

## 3. Data model, contracts & events (new)

### 3.1 New tables (owned by the orchestration host; RLS tenant-scoped)
Honor the spec's names so a future ax-2 extraction is drop-in:

```sql
-- xagent.workflows  (one orchestration run)
workflow_id UUID PK, tenant_id, root_agent_id (orchestrator), goal TEXT,
status ∈ {pending,planning,running,awaiting_approval,completed,failed,cancelled,timeout},
mode ∈ {solo,subagents}, decomposition ∈ {template,llm}, subtask_dag JSONB,
output JSONB, error_code, error_msg,
tokens_used INT, cost_usd NUMERIC, cost_budget_usd NUMERIC,   -- budget ceiling (§7 #4)
approval_due_at TIMESTAMPTZ, created_at, started_at, completed_at, timeout_at,
version INT DEFAULT 1  -- optimistic lock

-- xagent.workflow_tasks  (one DAG node)
id UUID PK, workflow_id FK, tenant_id, node_id TEXT, task_id UUID (the spawned ax-1 task),
parent_node_id, description TEXT, node_type ∈ {task,agent,tool,approval,condition,join},
assigned_agent_id UUID (the sub-agent), preset TEXT NULL, depends_on UUID[]/TEXT[],
status ∈ {pending,running,awaiting_approval,completed,failed,cancelled,timeout},
output JSONB (summary+citations), tokens_used, cost_usd, retry_count, retry_max DEFAULT 1,
version INT DEFAULT 1

-- xagent.agent_presets  (§7 #8 — the ".claude/agents" analogue; optional but recommended)
preset_id UUID PK, tenant_id, name, description, system_prompt, model_alias,
allowed_tools TEXT[], allowed_scopes TEXT[], created_at
```
Plus a small addition to `xagent.tasks`: `parent_task_id UUID NULL` + `workflow_id UUID NULL` (lineage; NULL for normal single-agent tasks).

### 3.2 Orchestrator roster/allowlist (your requirement #7)
Add to the orchestrator's config (Auth side, on `auth.agents.metadata` or a small `auth.orchestrator_roster` table):
`usable_sub_agents: 'all' | string[]` + `max_sub_agents` + `max_fanout` + `max_depth`. Default `all`. Restrictable in the UI. Read by the decomposition/assignment step to bound which agents may be delegated to.

### 3.3 New contracts (author under `contracts/`)
- `contracts/workflows/dag.schema.json` — nodes (`node_type`, `ref`, `input_bindings`, `timeout_seconds`, `retry`), edges (`from`,`to`,`condition?`) with acyclic constraint; `constraints{max_depth,max_fanout,max_cost_usd,max_tokens}`; `hil_gates[]`.
- `contracts/workflows/run.schema.json` — run + subtask tree (`run_id`, `root_task_id`, `status`, `tasks[]{task_id,parent_task_id,node_id,assigned_agent_id,status,cost_usd}`).
- Add OPTIONAL `workflow_id`/`parent_task_id` to `a2a/task-request.schema.json` (already reserved).
- Tag `x-enforcement-phase: phase-10`. Update `scripts/check-structure.mjs` manifest.

### 3.4 Events (reuse the `xagent.outbox`)
`cypherx.agent.workflow.completed` / `.failed` / `.approval.recorded` / `.node.completed` — all carry `request_id`+`trace_id`+`workflow_id` (Contract-5 envelope). Enables the usage/audit joins and the live tree fan-in.

### 3.5 Run SSE event shape (extends the existing task stream)
The run stream emits node-attributed frames so the UI can build a tree:
`event: node` `{node_id, assigned_agent_id, status, step?, tokens?, cost_usd?}` · `event: approval` `{request_id, node_id, operation_type, context}` · `event: run` `{status, tokens_used, cost_usd}` · `event: done` `{result}`. (The current flat `TaskStep` frames stay for single-agent runs.)

---

## 4. Backend plan (phased)

> Host = ax-1 `orchestration/` module unless you pick ax-2. Each phase ends green (pytest + ruff + mypy) and is built by a sub-agent, then hardened by an **adversarial-review workflow** (our proven pattern — it consistently finds real bugs the passing tests miss).

**B0 — Foundations & the cross-agent seam.**
- Migrations for `xagent.workflows`, `xagent.workflow_tasks`, `xagent.agent_presets`, `xagent.tasks.parent_task_id`+`workflow_id`.
- Add an **internal execution entrypoint** in ax-1 that runs the pipeline for an *arbitrary target agent in the same tenant* — **without** relaxing the public `/v1/tasks` caller-vs-target rule. This is a new internal function (not the public route): `run_agent_pipeline(target_agent_id, input, on_behalf_of, budget, cancel, parent_task_id, workflow_id)`. Reuses `LoadStage`→`EventStage`. Downstream identity = `on_behalf_of=<target>`.
- Authorization guard: the caller must be the tenant orchestrator; `target_agent_id.parent_orchestrator_id == orchestrator` (or target ∈ roster allowlist). Cross-tenant impossible (RLS).

**B1 — Decomposition.** ⚠️ *REVERSED 2026-07-13. The original spec is struck through; what shipped is below it.*

- ~~`Decomposer`: goal → DAG. **Template router** first (a table of preset shapes: `single`, `sequential-pipeline`, `parallel-fanout+synthesis`, `research→write→review`); **LLM `plan` fallback** only for novel goals.~~ **DELETED.** The template router (`match_template()`) *was* forced routing: an `if` chain mapping goal substrings to a fixed set of sub-agents, with the LLM never consulted. Being a substring matcher it could not read a negation, so *"…and do NOT write a brief"* matched the `write` keyword and emitted a brief-writing step regardless. Re-introducing it is forbidden.

- **What shipped — the planner decides, the backend validates:**
  1. `Decomposer`: goal → the orchestrator's **LLM planner**, which is shown a **capability catalogue** of the tenant's real sub-agents (`name` + `description` + actual `allowed_tools`) plus an `orchestrator` target meaning *"no delegation"*. It returns `{"steps":[{id, step, preset, depends_on}]}`. `plan_to_dag()` translates it **mechanically** — nothing inferred, nothing defaulted.
  2. **Validate** (never re-route): **Kahn cycle-check** + caps (depth ≤ `max_depth`, fanout ≤ `max_fanout`) → `INVALID_DAG`; and `validate_targets()` → **`UNKNOWN_AGENT`** if a step names an agent that does not exist. No nodes spawned on failure.
  3. **Repair, don't substitute:** an invalid plan goes **back to the planner once**, with the exact reason and the valid target list — gated by a HIL approval (an explicit human *deny* hard-fails; an unreachable HIL retries anyway). A second failure → **`ORCHESTRATION_FAILED`**. The backend never picks a replacement agent.
  4. Persist in `subtask_dag`; record `decomposition ∈ {template,llm}`, where **`template` now means only `solo`** (the single no-delegation node) and every delegating graph is `llm`.
  5. **Delegation is the exception, not the default.** The planner's system prompt states the default is *no* delegation — delegate only for parallelism, specialization, or isolation. A one-step plan is a good plan.

**B2 — DAG driver + fan-out + join.**
- Background async job (mirror ax-1's async-task driver + `_track_background_task` + sweeper): topological execution, in-degree-0 nodes via `asyncio.gather(return_exceptions=True)`, optimistic-locked node transitions (`version`), state+events atomic via outbox. Retry (`retry_max` default 1) + reuse the Phase-9 per-target circuit breaker.
- Each node → `run_agent_pipeline(...)` for its `assigned_agent_id`. **Summary-only return**: node `output` = `{summary, citations}` (bounded by the 256 KiB cap); the orchestrator's synthesis reads `output`, never child transcripts.

**B3 — Budget ceiling + cancel + timeout.**
- Per-workflow `cost_budget_usd` / token ceiling: accumulate node `usage_records` (in Valkey keyed by `workflow_id`); on breach → trip cancel (early stop). Workflow-level `timeout_at` + sweeper backstop. Cancel propagates to all running node tasks (reuse ax-1's Valkey cancel per child task).

**B4 — HIL gating (finish the STUB).**
- Before spawning a `sub_agent`/risky-`tool` node, the driver calls Auth `POST /v1/hil/approvals/request` with `operation_type` (`sub_agent_creation`/`tool_execution`). If pending → node + workflow → `awaiting_approval`, emit `approval` SSE frame, pause. On grant → resume; on deny/expiry → cancel that branch (policy: fail the workflow or skip node — configurable).
- Also wire the **same gate into `OrchestratorService.createSubAgent`** so manual sub-agent creation honors `human_in_loop`/`partial` (closes the known STUB).

**B5 — Synthesis + run API + SSE.**
- `SynthesisStage`/step: orchestrator LLM composes final `output.message` from child summaries (+ optional citation-verification pass as an **opt-in** preset, §7 "build later").
- Endpoints: `POST /v1/orchestrations` (submit goal, mode, budget → `run_id`), `GET /v1/orchestrations/{id}`, `GET /v1/orchestrations/{id}/graph`, `GET /v1/orchestrations/{id}/stream` (SSE), `DELETE /v1/orchestrations/{id}` (cancel), `GET /v1/orchestrations` (list).
- Presets CRUD: `GET/POST/PATCH/DELETE /v1/agent-presets`.

**B6 — Efficiency features (the "worth-it" set, §7).**
- Prompt caching (`cache_control: ephemeral`) on the orchestrator's fixed prefix at the LLM gateway. Result/idempotency cache (`wf-result:{hash(node_input)}`). Per-sub-agent least-privilege tool/scope attach per node/preset. `immutable_llm` enforcement (ax-1 PUT-runtime guards `llm_model` when set; or drop the flag).

---

## 5. Frontend plan (phased)

**F0 — Session + nav plumbing.**
- Add `agent_id` to the frontend `Session` type + `/bff/me` passthrough (server already has it). If ax-2 host: add the upstream (config) + extend `isStreamRoute`. If ax-1 host: reuse `xagent` upstream + its existing stream route (no BFF code change).
- Add `services.ts` wrappers: `submitOrchestration`, `getOrchestration`, `getOrchestrationGraph`, `cancelOrchestration`, `listAgentPresets` + CRUD; `streamUrl` for the run.

**F1 — Orchestrator Run experience** (new `orchestrator/run/page.tsx`, modeled on `tasks/run`).
- Prompt box + **"Use sub-agents" toggle** (your req #5) + **sub-agent selector** showing the orchestrator's usable roster (your req #8, powered by `listSubAgents()` filtered by roster) + budget/max-fanout inputs.
- On submit → `EventSource` run stream → live **Execution Tree**.

**F2 — Execution Tree component** (`ExecutionTree.tsx`, new — nothing reusable is hierarchical).
- Renders the DAG from node frames: per-node status, assigned sub-agent, tokens, cost; expand a node to see its `Pipeline`/`TaskTimeline` (reuse both as leaf rails) and its summary. Root shows aggregate tokens/cost vs budget. This is the "reads like Claude" activity tree.

**F3 — The toggle-OFF + intent → HIL settings flow** (your req #6).
- When "Use sub-agents" is **OFF** but the prompt clearly asks for sub-agents (lightweight client-side intent hint + server confirmation), show a **Sub-Agent Settings** dialog (HIL-style gate) listing **Available agents** (roster) and **max sub-agents / depth / fanout**, with Enable-for-this-run / Cancel. Enabling flips the run to `subagents` mode. This reuses the HIL visual language and the roster config.

**F4 — Inline HIL during a run.**
- Surface `approval` SSE frames inside the tree with Grant/Deny (reuse `grantHilApproval`/`denyHilApproval`); the workflow resumes on the same stream. (Today `/hil` has no polling/stream — this is the first live-approval surface.)

**F5 — Per-sub-agent settings + presets + roster admin.**
- Reuse `AgentBuilder` for a sub-agent's runtime (it's `agent_id`-keyed → works unchanged) + add `immutable_llm` handling (disable the model select when locked). Add a **Presets** manager (researcher/writer/reviewer bundles). Add **roster/allowlist** controls to `/orchestrator` (default all; restrict which agents are delegatable + caps — your req #7).

---

## 6. The task-runner sub-agent UX (your requirements #5–#8, precisely)

| Your requirement | Implementation |
|---|---|
| **#5** Sub-agents **ON** → "use sub-agents" added to the prompt, session runs orchestrated | Run in `mode=subagents`: orchestrator decomposes + delegates. The toggle sets `mode` on `POST /v1/orchestrations`. |
| **#6** Sub-agents **OFF** but user asks for sub-agents → HIL → show sub-agent settings (available agents, max) | Intent hint → **Sub-Agent Settings gate** (F3): lists available agents + max caps; Enable-for-run or Cancel. A human-in-the-loop confirmation before spending on fan-out. |
| **#7** Created agents auto-added to orchestrator; default all usable; restrictable | Orchestrator **roster/allowlist** (§3.2): default `all`; UI to restrict which agents are delegatable + set `max_sub_agents/fanout/depth`. |
| **#8** Task runner shows usable sub-agents | Run page renders the roster (from `listSubAgents()` ∩ roster) as the sub-agent selector; the tree shows which ran. |
| Orchestrator maintains a **tree/graph** | `xagent.workflow_tasks` DAG + `GET /orchestrations/{id}/graph` + `ExecutionTree` live view. |
| **Only orchestrator** creates/runs sub-agents | Already enforced (Auth `requireOrchestrator`, depth-1); the run endpoint requires the orchestrator identity. |
| Each sub-agent has **its own settings, scoped to orchestrator** | `AgentBuilder` per sub-agent + create-time scope subset (REAL) + presets. |

---

## 7. "Just like Claude" — the worth-it feature list (cost-filtered)

Ranked, adversarially filtered. **★ = build now, zero/negative extra $, high ROI.**

**Build now (high ROI, low cost):**
1. ★ **Summary-only sub-agent returns** — orchestrator ingests each child's *summary + citations*, never its transcript. The single biggest token lever (net-negative cost). Reuses the 256 KiB output cap. *(§4 B2)*
2. ~~★ **Deterministic/template decomposition** — skip the planning LLM call for common shapes.~~ **NOT BUILT — deliberately rejected 2026-07-13.** This was forced routing, and the token it saved was not worth it: a substring router cannot judge whether delegating is warranted at all, and cannot read a negation. **What replaced it:** the planner always runs, but its prompt makes *not delegating* the default ("if ONE agent can do the whole job, emit exactly ONE step"), which kills the over-spawn blow-up at the source rather than by pre-empting the model. **Scale-effort caps SHIPPED** (hard depth/fanout ceilings, enforced in `dag.py` and quoted to the planner). *(§4 B1)*
3. ★ **Live execution tree with per-node tokens/cost** — the "reads like Claude" UX at **zero token cost**; reuses SSE + `workflow_tasks`. *(§5 F2)*
4. ★ **Per-workflow budget ceiling + early stop** — sum `usage_records` by `workflow_id`, trip the cancel path on breach. ~50 lines of catastrophe insurance. *(§4 B3)*
5. ★ **Prompt caching on the orchestrator's fixed prefix** — one `cache_control: ephemeral` marker → ~90% cheaper / ~85% faster on repeated turns. Free money at the gateway. *(§4 B6)*
6. **Parallel fan-out** of independent nodes (`asyncio.gather`) — the only reason multi-agent wins; cuts wall-clock up to ~90%. *(§4 B2)*
7. **Per-sub-agent least-privilege tools/scopes** — reuses scope subset + tool ACLs; smaller prompts + fewer wrong-tool errors. *(§4 B6 / §5 F5)*
8. ~~**Sub-agent presets** (researcher/writer/reviewer) — the `.claude/agents` analogue.~~ **REVISED 2026-07-13.** There are **no reserved role names**. What shipped is the *useful* half of the `.claude/agents` analogue: every sub-agent carries a **`description`** ("when to use this agent") alongside its **tools**, and the planner routes on that pair — exactly as Claude Code routes on a subagent's `description` frontmatter. Names are arbitrary and carry no meaning. Seeding a fixed researcher/writer/reviewer trio was what made the router possible in the first place. *(§3.1, §5 F5)*
9. **Finish HIL inline + streaming + cancel in the tree** — mostly already built; makes the tree trustworthy. *(§4 B4, §5 F4)*
10. **Bounded retry + per-target circuit breaker** — cheap reliability; `retry_max` already in the schema. *(§4 B2)*

**Build later (real, not first-cycle):** dry-run/plan-preview before executing the DAG · **critic/verifier pass** (opt-in preset only — a full extra LLM pass ≈ 2× cost, never default) · shared workflow-scoped mutable memory · conditional/loop nodes · dynamic model auto-tiering.

**Reject (tempting, not worth it):** full Temporal-lite durable engine (outbox + optimistic-lock + timeouts already cover ~90%) · heavyweight A2A for internal calls · per-agent K8s services · cross-tenant federation · per-agent heartbeats · vector-DB for agent routing · GroupChat turn-selection · unbounded swarm fan-out.

> Guiding number: multi-agent costs ~**15× a single chat** and token spend explains ~80% of quality variance — so keep **single-agent the default**, spawn sub-agents only when the goal genuinely decomposes into independent parallel threads, and let the caps + budget ceiling bound the downside.

---

## 8. Security, scopes & invariants to honor

- **Reuse existing scopes:** `orchestrator:manage` (roster/sub-agent admin + run), `hil:approve` (resolve approvals), `agent:execute` (run). No new scope needed for the internal path.
- **Never break** ax-1's public `/v1/tasks` caller-vs-target rule — orchestration uses a *separate internal entrypoint*, not a relaxed public route.
- **RLS everywhere** — every workflow/node query sets `app.tenant_id`; cross-tenant is invisible (404, not 403).
- **Confinement is defense-in-depth** — create-time scope subset (Auth) **and** LLM alias allowlist (LLMs gateway) **and** tool access grants (Tools registry), all keyed off `on_behalf_of=<sub_agent_id>`.
- **A2A stays external-only** — do not build the a2a-router/delegation chain for the internal path. Reserve the schemas + honor Component 8d rules *only if/when* cross-vendor federation is built.
- **Honor persisted tokens** (don't rename without migration): `agent_type` values, `orchestrator_hil_config`, trigger names (`tool_execution|sub_agent_creation|llm_restriction|skill_execution`), `SUB_AGENT_CANNOT_DELEGATE`, `LLM_ALIAS_NOT_ALLOWED`.

---

## 9. Phasing & build order (subagent-driven)

Dependency-ordered; each is one sub-agent build + one adversarial review.
1. **Contracts** (§3.3) — DAG/run schemas + a2a extension fields + structure-gate update. *(unblocks everyone)*
2. **B0** migrations + internal execution entrypoint + authz guard.
3. **B1** decomposer (template + LLM fallback + Kahn/caps).
4. **B2** DAG driver + fan-out/join + summary-return.
5. **B3** budget ceiling + cancel/timeout.
6. **B4** HIL gating (+ close `createSubAgent` STUB).
7. **B5** synthesis + run API + SSE.
8. **F0–F2** session/nav + run page + execution tree (can start in parallel after §3.5 SSE shape is frozen).
9. **F3–F4** toggle-off→HIL-settings + inline approvals.
10. **B6 / F5** efficiency features + presets + roster admin + `immutable_llm`.
11. **Adversarial review pass** over the whole surface (find→verify→fix), then `/verify` end-to-end on a real tenant.

Parallelizable tracks after step 2: **backend engine** (3→7) and **frontend shell** (8) proceed concurrently against the frozen §3 contracts/SSE shape.

---

## 10. Testing & verification

- **Unit/integration (pytest, network-free):** decomposition (template + LLM), Kahn cycle-reject, fan-out/join with optimistic-lock races, budget early-stop, cancel propagation, HIL pause/resume, summary-only return, roster/authz (orchestrator-only, own-sub-agent-only, cross-tenant 404).
- **Determinism/property tests** on the DAG driver (same DAG → same execution set; idempotent replay).
- **Auth (Kotlin) tests** — the missing `OrchestratorIntegrationTest`/`HilIntegrationTest` (currently untested) + the new `createSubAgent`→HIL gate.
- **Frontend (vitest)** — run page, tree rendering from mock SSE frames, toggle-off→settings gate, inline approval.
- **Adversarial-review workflow** on every correctness-critical module (our standard — it's caught real bugs the green tests missed every prior phase).
- **`/verify`** end-to-end: submit a goal with sub-agents ON → watch the tree → hit a HIL gate → grant → see synthesis + real tokens/cost.

---

## 11. Open decisions (please confirm at review)

1. **Host: ax-1 module (recommended, lower complexity) vs new ax-2 service (cleaner, spec home).** §0. *Recommendation: ax-1 for MVP, extract to ax-2 if/when scaling demands.*
2. **Default when a sub-agent HIL is denied mid-run:** fail the whole workflow, or skip that node and continue with partial results? *Recommendation: skip-node + mark partial for `research`-type; fail for `tool`/write-type.*
3. **`immutable_llm`:** enforce it (sub-agent's model locked at creation) or drop the flag? *Recommendation: enforce — it's a cheap least-surprise guard and it's already plumbed.*
4. **Critic/verifier pass:** ship as an opt-in preset now, or defer entirely? *Recommendation: defer to "build later"; it ~doubles cost.*
5. ~~**Presets:** ship the 3 defaults (researcher/writer/reviewer) seeded per tenant, or let tenants define their own only?~~ **RESOLVED 2026-07-13 — tenants define their own, full stop.** No seeded roles, no reserved names. Each sub-agent declares a **`description`** ("when to use this agent"); the planner routes on that plus the agent's real tools. Seeding a fixed trio is what let a keyword router exist, and a router that picks the agent is the one thing this engine must never do.

---

*End of plan. On approval, implementation proceeds subagent-by-subagent in the §9 order, each gated by an adversarial review, nothing merged un-verified.*
