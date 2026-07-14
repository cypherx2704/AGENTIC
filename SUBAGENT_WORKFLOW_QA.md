# Sub-Agent Workflow — Short Answers

> Verified against the code on branch `sub-agent-completed` (2026-07-14).
> Long-form version: [SUBAGENT_WORKFLOW_EXPLAINED.md](SUBAGENT_WORKFLOW_EXPLAINED.md).
> **Governing rule: the LLM planner decides; the backend only validates.**

---

## 1. How does the LLM split the task and assign it to sub-agents?

One planning call, one JSON plan, translated mechanically into a DAG.

The orchestrator's own LLM returns:

```json
{"steps": [{"id": "...", "step": "what this step does", "preset": "<target agent>", "depends_on": ["..."]}]}
```

- **`preset` is the assignment** — the name of a real sub-agent, or the reserved `orchestrator` target meaning *"no delegation, I answer this myself"*.
- **`depends_on` is the split** — `plan_to_dag()` ([decompose.py:131](xAgent/ax-1/src/agent_runtime/orchestration/decompose.py#L131)) turns each step into one node and each dependency into one edge. Nothing is inferred or defaulted. Steps with no dependency between them land in the same topological layer and run **concurrently** (`asyncio.gather`); dependent steps become later layers and receive their upstreams' summaries as input.
- Splitting is *discouraged by default*: the prompt says "If ONE agent can do the whole job, emit exactly ONE step. That is a good plan, not a lazy one."

## 2. How does the LLM intelligently choose the sub-agents?

It routes on a **capability catalogue** of the tenant's real sub-agents ([llm.py:89-108](xAgent/ax-1/src/agent_runtime/orchestration/llm.py#L89-L108)) — never on names:

```
- gh-researcher
    use when: Fetches stars, open issues and release history for a GitHub repository.
    tools: tool-github-stats-<tenant>
- writer
    use when: Turns findings into prose. Performs no lookups of its own.
    tools: NONE (cannot call any tool)
- orchestrator
    use when: no sub-agent is needed — you answer the step yourself
    tools: NONE
```

Two signals, each doing a job the other cannot:

- **`description`** ("when to use this agent") — the *intent* signal. It is the only thing that separates two toolless agents (a `writer` and a `reviewer` both show `tools: NONE`).
- **`allowed_tools`** — the *ground-truth* constraint. A step needing external data, routed to an agent with no tool to fetch it, does not fail — it **fabricates**. So the prompt hard-rules: a step needing external data may only go to an agent whose tools can fetch it.

This is the same mechanism Claude Code uses (a subagent's `description` frontmatter). There are **no reserved role names** — no researcher/writer/reviewer trio, no keyword router. If nothing fits, the planner is told to say so in an `orchestrator` step rather than fake it.

## 3. Does any backend code force the LLM to use the configured sub-agents?

**No.** I checked every place the backend could sneak a routing decision in; none of them picks an agent.

| Backend touchpoint | What it does | Routing? |
|---|---|---|
| Planner system prompt | **Describes** the roster; states the default is *no* delegation | No — describes choices, never prescribes one |
| `validate_targets()` | Rejects a step naming a non-existent agent (`UNKNOWN_AGENT`) or naming none at all | No — reports, never replaces |
| `resolve_node_agent()` | Unknown name **raises `UNKNOWN_AGENT`**; it deliberately does *not* fall through to a default | No — that loud-failure branch exists precisely to prevent substitution |
| `validate_dag()` | Kahn cycle check + hard caps (depth ≤ 5, fanout ≤ 8; a DAG may only *lower* them) | No — bounds blast radius |
| Repair loop | Invalid plan goes **back to the planner once** with the reason; second failure → `ORCHESTRATION_FAILED`, run dies | No — the backend never picks a replacement |
| Roster query | Filters to active `sub_agent`s owned by this orchestrator with a runtime row | **Availability/authz filter** — bounds what *exists*; the LLM still picks from it |
| HIL gate | A human may deny a delegation | Governance, not routing |
| `mode: 'solo'` | Skips the planner; one orchestrator node | The **user's** toggle on the run page, not a backend choice |
| `planner is None` | No LLM wired → solo graph | Degenerate case: with no model there is nobody to decide, and the backend refuses to decide *for* it — so it delegates to nobody rather than guessing |

The only graph the backend builds itself is `solo`: one node, no `preset`, bound to `default_agent_id`, which is **always the orchestrator** — never a sub-agent. So even the fallback cannot put work on an agent the planner did not choose.

**The forced-routing code did exist and was deleted** — `match_template()`, an `if`-chain mapping goal substrings onto a fixed `researcher → writer → reviewer` shape. It was removed because a substring matcher cannot read a negation: *"…and do NOT write a brief"* matched the `write` keyword and produced a brief-writing step anyway. A regression test (`test_no_keyword_router_or_preset_templates_exist`) now fails if any of those names return, and `xagent.agent_presets` was dropped in migration `20260714_0010`.

## 4. End-to-end walkthrough

**Setup (once):** create each sub-agent on the Orchestrator page — Auth writes the identity (`agent_type='sub_agent'`, `parent_orchestrator_id`, scope subset), xAgent writes the runtime (`status='active'`, **`description`**, `system_prompt`, `llm_model`, `allowed_tools`). That description + tool list is the entire basis on which the planner will later route.

**Run:**

1. **Submit** — `POST /v1/orchestrations {goal, mode, cost_budget_usd?, timeout_seconds?}`. Orchestrator JWT only (a sub-agent gets 403 — delegation depth is capped at 1). A `pending` workflow row is inserted, `drive()` is fired as a background task, **202** returns immediately. The run page's "use sub-agents" toggle is what sets `mode`.
2. **Roster** — active, owned sub-agents with runtime rows, plus the `orchestrator` pseudo-target. **Zero sub-agents is fine**: the planner simply routes everything to `orchestrator`.
3. **Plan** (status `planning`) — the orchestrator's own model and JWT (so planning is confined to, and billed against, the orchestrator). Planning tokens are accrued to the run total *even if the run later fails*.
4. **Validate** — mechanical `plan_to_dag()` → acyclic + depth/fanout caps → every step names a real agent. Invalid → back to the planner **once** with the exact reason, gated by a HIL approval (explicit human *deny* hard-fails; an unreachable HIL retries anyway). Second failure → `ORCHESTRATION_FAILED` with **zero sub-agents spawned**. The DAG is persisted to `workflows.subtask_dag`.
5. **Execute** (status `running`) — one `workflow_tasks` row per node, then layer by layer:
   - between layers: poll the Valkey cancel flag and the wall-clock deadline;
   - nodes whose dependencies did not all succeed are cascade-skipped;
   - ready nodes run concurrently. Each delegating node passes a HIL gate (`sub_agent_creation`) — a node routed to the orchestrator itself skips it, since there is no delegation to approve.
6. **Run a node** — mint a scoped sub-agent JWT via Auth's delegation endpoint (cached to the token's real `exp`), then run the *existing* single-agent pipeline (`LOAD → PRE_GUARDRAIL → PROMPT_BUILD → TOOL_LOOP → LLM → POST_GUARDRAIL → EVENT`) as a real child `xagent.tasks` row with `workflow_id`/`parent_task_id` set — so it inherits the whole ax-1 reliability envelope (timeout, cancel, sweeper recovery, audit steps, terminal Kafka event). **No A2A** — in-tenant, in-process. It returns **summary + citations only; never the transcript** (the biggest token lever).
7. **Budget/failure** — node spend accumulates (seeded with the planner's). Crossing `cost_budget_usd` stops the run after the current layer with `BUDGET_EXCEEDED`; each node is separately capped at the *remaining* budget so one node cannot burn the lot. Node failures follow `on_error` (`fail` aborts, `skip`/`continue` proceeds with partial results).
8. **Synthesize** — the orchestrator LLM composes the final answer from the node summaries ("use ONLY the findings provided"). On error/no-LLM it falls back to a deterministic join of the leaf summaries, so a run always yields output.
9. **Finalize** — `completed | failed | cancelled | timeout` with `output.message`, `tokens_used`, `cost_usd`. Every path finalizes; a run is never left non-terminal.

**Reading a run:** `GET /v1/orchestrations/{id}` · `/graph` (run + node tree) · `/stream` (SSE, emits a frame whenever the graph changes, closes on terminal) · `DELETE` (cancel; tears down in-flight nodes).
