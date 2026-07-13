# Sub-Agent Workflow — How It Works

> The internal (non-A2A) `PROMPT → ORCHESTRATOR → SUB-AGENTS` engine in
> [xAgent/ax-1/src/agent_runtime/orchestration/](xAgent/ax-1/src/agent_runtime/orchestration/).
>
> Companions: [SUBAGENT_WORKFLOW_PLAN.md](SUBAGENT_WORKFLOW_PLAN.md) (design + phases),
> [xAgent/ax-1/CLAUDE.md](xAgent/ax-1/CLAUDE.md) (the single-agent runtime every sub-agent runs on).

---

## The governing rule

**The LLM decides which sub-agent runs what. The backend only validates.**

No `if` statement anywhere picks an agent. No keyword maps a goal to a shape. No default agent is
substituted when the planner errs. If the planner cannot produce a usable plan, the run **fails** —
it does not quietly fall back onto something the planner never chose.

Three things follow from that, and they are the whole design:

| The backend MAY | The backend MAY NOT |
|---|---|
| Show the planner the roster (names, descriptions, tools) | Tell it which one to use |
| Reject a plan that names a non-existent agent | Pick a replacement for it |
| Reject a cyclic or over-wide graph | Rewrite the graph to fit |
| Hand the rejection back to the planner to fix | Fix it itself |
| Fail the run when no valid plan arrives | Invent a plan so the run "succeeds" |

---

## 1. How the planner splits the task

One LLM call, under the **orchestrator's own model and JWT** (so planning is confined to, and billed
against, the orchestrator — not a sub-agent):

| role | content |
|---|---|
| `system` | capability catalogue + delegation policy + routing rules + graph limits + output schema |
| `user` | `Goal:\n<the raw goal>` |

It replies with JSON, which `plan_to_dag()` translates **mechanically** — one step becomes one node,
one `depends_on` becomes one edge. Nothing is inferred, nothing is defaulted:

```json
{"steps": [
  {"id": "stars",  "step": "Look up the repo's stars and open issues", "preset": "gh-researcher", "depends_on": []},
  {"id": "issues", "step": "Summarise the top 5 open issues",          "preset": "gh-researcher", "depends_on": []},
  {"id": "brief",  "step": "Write a 200-word brief from the findings", "preset": "writer",        "depends_on": ["stars", "issues"]}
]}
```

`dag.validate_dag()` then checks it: acyclic (Kahn), depth ≤ 5, fan-out ≤ 8, and — via
`validate_targets()` — that **every step names a real agent**. Valid plans become topological layers;
each layer runs concurrently.

There is no step-count quota. **One step is a legal, encouraged plan.**

## 2. How the planner chooses a sub-agent

It routes on **two signals**, because neither works alone:

- **`description`** — *"when to use this agent"*, authored by you. The **intent** signal. It's the only
  thing that can distinguish two agents holding identical tools, and the *only thing at all* for
  toolless agents — a `writer` and a `reviewer` both show `tools: NONE`.
- **`allowed_tools`** — what the agent can physically do. The **ground-truth constraint**. A step
  needing external data, sent to an agent with no tool to fetch it, doesn't fail — it **fabricates**.

Rendered into the prompt as:

```
AVAILABLE TARGETS (route by CAPABILITY — read BOTH the description and the tools):
- gh-researcher
    use when: Fetches stars, open issues and release history for a GitHub repository.
    tools: tool-github-stats-<tenant>
- writer
    use when: Turns findings into clean prose. Performs no lookups of its own.
    tools: NONE (cannot call any tool)
- orchestrator
    use when: no sub-agent is needed — you answer the step yourself
    tools: NONE (cannot call any tool)
```

`description` is a **required field** when you create a sub-agent. Leave it empty (only possible for
rows predating migration 0009) and the planner is told so plainly —
`use when: UNSPECIFIED — no description was configured for this agent` — rather than being fed a
meaningless default that would have it guess from the name.

> **Note on the two prompts.** `description` is written *for the router* ("use me to fetch GitHub
> stats"). `system_prompt` is written *for the agent* ("be terse, always cite"). Different audiences.
> Conflating them was the original bug: the system prompt defaulted to *"You are a helpful assistant.
> Answer concisely."*, so every sub-agent advertised the same useless purpose.

## 3. How the planner decides **not** to delegate

`orchestrator` is a first-class routing target, always present in the catalogue — even for a tenant
with zero sub-agents. The delegation policy makes declining the default:

> **The DEFAULT is NO delegation.** Delegate only for (a) **parallelism** — genuinely independent
> work; (b) **specialization** — the step needs a tool or expertise a specific agent has; or
> (c) **isolation** — the step generates a lot of intermediate material better distilled to a summary.
> *If ONE agent can do the whole job, emit exactly ONE step. That is a good plan, not a lazy one.*
> NEVER add a step the goal did not ask for. Obey NEGATIONS exactly.

A no-delegation plan is one step with `"preset": "orchestrator"`. The executor sees
`sub_agent_id == orchestrator.agent_id`, skips the token mint, and runs it under the orchestrator's
own JWT. No sub-agent is spawned, and the HIL gate (which approves *sub-agent creation*) is skipped —
there is nothing to approve.

## 4. How concurrency is decided

**By the planner, via `depends_on`.** Steps with no dependency between them land in the same
topological layer and run concurrently under `asyncio.gather`. `topological_layers()` is pure Kahn's
algorithm over the graph the planner authored — it *executes* the declared concurrency, it does not
infer, bucket, or categorise anything. There is no "research steps run in parallel" rule anywhere.

`max_fanout = 8` / `max_depth = 5` are anti-runaway **ceilings**, not routing decisions — and the
prompt now quotes those exact numbers, so a plan is never rejected for a rule the model wasn't told.

## 5. When the planner gets it wrong

```
plan → validate (cycle? depth? fanout? every target real?)
   │
   ├── valid ─────────────────────────────────► execute
   │
   └── invalid
         │
         ├─ ask the human: "the plan was rejected because <reason>. Re-plan?"   [HIL]
         │     ├─ DENIED       → HARD-FAIL: ORCHESTRATION_FAILED
         │     ├─ GRANTED      → re-plan
         │     └─ UNAVAILABLE  → re-plan   (HIL off / Auth down / expired / nobody answered)
         │
         └─ re-plan ONCE, feeding the model the exact reason + the valid target list
               ├─ valid   → execute
               └─ invalid → HARD-FAIL: ORCHESTRATION_FAILED
```

The repair turn is a real user message appended to the planning conversation:

> *Your previous plan was REJECTED and was NOT executed. Reason: Step 'brief' targets 'resercher',
> which is not an available agent. Valid targets: gh-researcher, orchestrator, writer.*
> *The ONLY valid "preset" values are: gh-researcher, orchestrator, writer.*
> *Re-plan… Reply with ONLY the JSON object.*

So even the planner's **mistakes** are fixed by the planner. The backend's role is to say precisely
what was wrong — never to fix it by choosing an agent itself.

`HilVerdict` is tri-state (`GRANTED` / `DENIED` / `UNAVAILABLE`) precisely so an explicit human "no"
can hard-fail while an unreachable approval service does not strand a run. `request_and_wait()` still
collapses everything non-granted to `False` for the tool-loop's fail-closed gate — unchanged.

---

## 6. End-to-end walkthrough

### Setup (once, by the tenant)

Create sub-agents on the [Orchestrator page](frontend/app/src/app/(app)/orchestrator/page.tsx). Each
creation is two writes (enforced by [subAgentRuntime.ts](frontend/app/src/lib/subAgentRuntime.ts)):

1. **Identity** → Auth `POST /v1/orchestrator/sub-agents` — sets `agent_type='sub_agent'`,
   `parent_orchestrator_id`, and a scope subset of the orchestrator's.
2. **Runtime** → xAgent `PUT /v1/agents/{id}/runtime` — `status:'active'`, **`description`** (required),
   `system_prompt`, `llm_model`, `allowed_tools`.

An identity without a runtime row can never be scheduled. The **description + tools you set here are
the entire basis on which the planner will route**. There are no roles, no presets, no reserved names.

### Run

1. **`POST /v1/orchestrations {goal}`** — orchestrator JWT only (`authz.require_orchestrator`; a
   sub-agent gets 403 — delegation depth is capped at 1). A `pending` workflow row is inserted;
   `drive()` is fired as a background task; **202** returns immediately.

2. **Roster** — active, owned sub-agents with runtime rows. **Zero is fine** — the planner will simply
   route everything to `orchestrator`.

3. **Plan** — status → `planning`. The planner call of §1. Rejected plans take the §5 repair path
   (status → `awaiting_approval` while a human is asked).

4. **Execute** — status → `running`. Per layer, in order:
   - poll the **cancel flag** (Valkey) and the **wall-clock deadline** → `cancelled` / `timeout`;
   - nodes whose dependencies didn't all succeed are **cascade-skipped**;
   - ready nodes run **concurrently**. For each:
     - **resolve**, exactly:
       ```
       if no agent is specified  ->  the default agent (the ORCHESTRATOR)
       elif that agent exists    ->  use it
       else                      ->  raise UNKNOWN_AGENT
       ```
       An agent the planner named but which does not exist raises **`UNKNOWN_AGENT`**, naming the
       bogus target and the real ones. It is **never** substituted, and never falls through to the
       default — that would discard the planner's decision and impose the backend's.
     - **HIL gate** — only when actually delegating;
     - **mint** a scoped sub-agent JWT (cached to its real `exp`), or reuse the orchestrator's own JWT
       when the node is the orchestrator's;
     - **create a child `xagent.tasks` row** (`parent_task_id`, `workflow_id`) so the node inherits the
       whole ax-1 reliability envelope — timeout, cancel, idempotency, sweeper recovery, audit steps,
       terminal Kafka event via the outbox;
     - **run the full single-agent pipeline** under the sub-agent's identity — its own system prompt,
       its own LLM↔MCP tool loop, its own guardrails and memory;
     - return a **summary only** (`{summary, citations}`). The transcript never leaves the sub-agent —
       the single biggest token lever in the design.
   - **budget**: node spend accumulates (seeded with the planner's). Crossing the ceiling stops the run
     after the current layer with `BUDGET_EXCEEDED`; each node is separately capped at the *remaining*
     budget so one node can't burn the lot.

5. **Synthesize** — every node summary, labelled, back to the orchestrator LLM ("use ONLY the findings
   provided"). No LLM / an error / empty → deterministic fallback: the leaf summaries, joined. A run
   always produces output.

6. **Finalise** — `completed` / `failed` / `cancelled` / `timeout`, with `output.message`,
   `tokens_used`, `cost_usd`. A failed run still records the planning spend it burned. No node is left
   non-terminal. The UI reads `GET /v1/orchestrations/{id}/graph` or streams `.../stream` (SSE).

### At a glance

```
POST /v1/orchestrations {goal}          (orchestrator JWT only)
      ▼
 workflows row = pending ──► 202 {workflow_id}
      │  background: coordinator.drive()
      │
 ┌─ ROSTER  xagent.agents WHERE parent=orch AND type=sub_agent AND active
 │     → [(name, description, tools)]  +  "orchestrator"      ← zero sub-agents is FINE
 │
 ├─ PLAN  (orchestrator's own model + JWT)          status=planning
 │     system: capability catalogue + "DEFAULT IS NO DELEGATION" + graph limits
 │     user:   the goal
 │     ──► {"steps":[{id, step, preset, depends_on}]}     ← the LLM's split AND routing
 │
 ├─ VALIDATE  acyclic · depth≤5 · fanout≤8 · every preset a REAL agent
 │     ✗ → [HIL: re-plan?] → DENIED: hard-fail │ else re-plan ONCE → still bad: hard-fail
 │
 ├─ EXECUTE  layer by layer, concurrent within a layer      status=running
 │     resolve:  no agent named → the orchestrator (default)
 │               agent exists   → use it
 │               else           → raise UNKNOWN_AGENT.  NEVER a substitute.
 │     → mint sub-agent JWT → child task → FULL ax-1 pipeline (own prompt, own tools)
 │     → summary-only result threads into downstream nodes
 │
 └─ SYNTHESIZE all summaries → workflows.output.message → completed
```

---

## 7. What was removed, and why

Every item below was a routing decision the backend made on the model's behalf. All are gone; a
regression test (`test_no_keyword_router_or_preset_templates_exist`) fails if any name comes back.

| Removed | What it did |
|---|---|
| `match_template()` | `if goal contains "compare" → parallel-research`. A forced routing rule. Substring-matched, so it could not read a negation: *"…and do NOT write a brief"* matched the `write` keyword and produced a brief-writing step anyway. |
| `TEMPLATES` (`research-write`, `research-write-review`, `parallel-research`) | Hardcoded graphs bound to reserved names `researcher` / `writer` / `reviewer`. |
| `PARALLEL_RESEARCH_BRANCHES = 3` | A backend-enforced 3-way fan-out. Concurrency is the planner's call. |
| `_PLAN_SYSTEM` | The roster-free prompt. Named a fixed `researcher\|writer\|reviewer` trio that might not exist, and demanded *"2-5 steps"* — structurally forbidding *"one agent is enough"*, so the model invented work to fill the quota. |
| The "no sub-agents → fail" gate | Killed a run with `UNASSIGNED_NODE` before the planner was ever consulted. The backend insisting sub-agents exist before it would let the model decide it needed none. |
| Silent default on an unknown preset | A hallucinated or mistyped agent name was quietly re-routed to the orchestrator. No error, no log — the plan's intent discarded and replaced by the backend's. It now raises **`UNKNOWN_AGENT`**. |
| Stale contract fixtures + plan spec | `contracts/workflows/examples/{dag,run}.json` still depicted a 3-node `research-write-review` graph as a `"template"` decomposition (now impossible), and `SUBAGENT_WORKFLOW_PLAN.md` still specified the keyword router as the design of record. Both corrected; the plan is stamped **PARTIALLY SUPERSEDED**. |
| `orchestratorPresets.ts` (frontend) | Seeded a fixed researcher/writer/reviewer roster whose names *had to* match `decompose.py`'s presets. |
| *"Use researcher / writer / reviewer to match the built-in plans"* (form hint) | Taught users to name agents after the dead templates. |

## 8. Files

| File | Role |
|---|---|
| [decompose.py](xAgent/ax-1/src/agent_runtime/orchestration/decompose.py) | Plan → DAG, validation, the repair loop. The only template left is `solo`. |
| [llm.py](xAgent/ax-1/src/agent_runtime/orchestration/llm.py) | The planner prompt (capability catalogue), plan parsing, synthesis. |
| [dag.py](xAgent/ax-1/src/agent_runtime/orchestration/dag.py) | Pure graph model: parse, cycle check, caps, topological layers. |
| [driver.py](xAgent/ax-1/src/agent_runtime/orchestration/driver.py) | Layer-by-layer execution, node binding, budget, cancel, HIL. |
| [executor.py](xAgent/ax-1/src/agent_runtime/orchestration/executor.py) | One node → one sub-agent task under its own minted JWT. Summary-only result. |
| [service.py](xAgent/ax-1/src/agent_runtime/orchestration/service.py) | The coordinator: roster → capabilities → planner → driver. Wires the HIL retry gate. |
| [repo.py](xAgent/ax-1/src/agent_runtime/orchestration/repo.py) | RLS-scoped persistence; `SubAgentRef.purpose` (description, else system prompt). |
| [hil_client.py](xAgent/ax-1/src/agent_runtime/services/hil_client.py) | `HilVerdict` tri-state: DENIED ≠ UNAVAILABLE. |
| [20260713_0009__subagent_description.sql](xAgent/ax-1/db/migrations/20260713_0009__subagent_description.sql) | The `description` column. |
