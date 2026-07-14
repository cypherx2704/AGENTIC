# Sub-Agent Workflow — Frontend Test Plan

> Click-through test cases for the whole sub-agent flow, run from the CypherX Console.
> Every case gives you the **exact config**, the **exact goal to paste**, and **what you should see**.
>
> Pages used: `/orchestrator` (manage sub-agents) · `/orchestrator/run` (run a goal) · `/hil` (approvals)
> · `/tools` (what tools exist) · `/tasks/[id]` (a sub-agent's own task detail).

---

## ⛔ TC-0 — PREFLIGHT (do this first, or every test below fails for the wrong reason)

Your roster is currently **broken in two ways**, both visible on `/orchestrator`:

| Column shows | Meaning | Consequence |
|---|---|---|
| **`No runtime`** | The identity exists in Auth, but there is **no row in `xagent.agents`** | The roster query reads that table, so **the planner cannot see this agent at all**. It will never be delegated to. |
| **`Not described`** | No routing description | Even once schedulable, the planner is routing **blind** — it can only guess from the name. |

**Fix (per agent):** click **Edit** → fill in **"When to use this agent"** → **Save**.
The Edit modal registers a runtime row when one is missing, so this fixes *both* problems in one action.

**PASS when:** every row shows `Schedulable: Ready` (green) and a real sentence under **When to use**.

> ⚠️ If **Repair/Edit** appears to succeed but the row still says `No runtime`, that is **not** proof the row
> is missing — the UI treats *any* error from the runtime probe (500, network, auth) as "no runtime". Open
> devtools → Network → look at `GET /bff/api/xagent/v1/agents/{id}/runtime` and read the real status.

---

## 🧰 The canonical roster (set this up once — every test below assumes it)

Check `/tools` for your tenant's real tool names first, then create/edit these four on `/orchestrator`.
The roster is deliberately built so **each routing signal is tested in isolation**.

| Name | When to use (description) | Tools | Why it's in the roster |
|---|---|---|---|
| `wiki-researcher` | *"Look up encyclopedic facts — history, people, places, definitions. Cites Wikipedia."* | wikipedia tool | Pairs with `repo-analyst`: **same job, different tool** → tests **tool-based** routing |
| `repo-analyst` | *"Fetch GitHub repository statistics: stars, forks, open issues, release history."* | github tool (or web-search) | ↑ |
| `brief-writer` | *"Turn findings that are given to it into a short, clean brief. Performs no lookups of its own."* | **none** | Pairs with `text-analyst`: **both toolless** → only the DESCRIPTION can tell them apart |
| `text-analyst` | *"Analyse text that is given to it — sentiment, themes, tone. Does not write new prose and does not look anything up."* | **none** | ↑ |

**Why this exact shape:** `wiki-researcher` vs `repo-analyst` can *only* be told apart by their **tools**.
`brief-writer` vs `text-analyst` both show `tools: NONE`, so they can *only* be told apart by their
**description**. If routing works for both pairs, both signals are live.

---

## A. Sub-agent management (`/orchestrator`)

### TC-A1 — Create a sub-agent
**Do:** New Sub-Agent → name `wiki-researcher` → description (above) → model `smart` → pick scopes → Create.
**Expect:** row appears, `Status: Active`, `Schedulable: Ready`, description visible under **When to use**.
**Red flag:** `No runtime` → the second write (xAgent runtime) failed; check the Network tab.

### TC-A2 — Description is mandatory
**Do:** New Sub-Agent → fill only the name → try to submit.
**Expect:** **Create is disabled.** You cannot make an undescribed agent.
**Proves:** you can't create an agent the planner would have to guess at.

### TC-A3 — Scopes are capped at the orchestrator's
**Do:** New Sub-Agent → open the scope picker.
**Expect:** only scopes the **orchestrator itself holds** are offered. A sub-agent can never out-scope its parent.

### TC-A4 — Edit a description after creation
**Do:** Edit on any agent → change **When to use** → Save.
**Expect:** toast "Sub-agent updated"; the **When to use** column reflects the new text.
**Proves:** the routing signal is editable — the gap that used to strand a repaired agent forever.

### TC-A5 — Repair an unschedulable agent
**Do:** Find an agent showing `No runtime` → **Repair** (or **Edit** → add a description → Save).
**Expect:** `Schedulable` flips to `Ready`.

### TC-A6 — Deactivate removes it from the roster
**Do:** Deactivate `text-analyst` → then run **TC-B2** (the toolless routing test).
**Expect:** the plan **never** routes to `text-analyst`. It is gone from the planner's catalogue.
**Proves:** deactivation mirrors into `xagent.agents` — a "deactivated" agent that kept getting scheduled would be a serious bug.

### TC-A7 — Non-orchestrator session is locked out
**Do:** Log in as a normal (non-orchestrator) agent → open `/orchestrator`.
**Expect:** warning callout, all management buttons disabled; `/orchestrator/run` refuses to submit.

---

## B. Routing — does the LLM pick the *right* agent? (`/orchestrator/run`)

> These are the heart of it. In each case, open the node's **Details** to confirm **which agent ran** and
> **which tool it called**.

### TC-B1 — Routing by **TOOL** (the "GitHub → wiki-researcher" bug)
**Goal to paste:**
> `How many open issues does the facebook/react repository have on GitHub?`

**Expect:** one step → **`repo-analyst`**, and a `tools:` chip showing the **GitHub tool**.
**FAIL if:** it routes to `wiki-researcher`. That agent holds only a Wikipedia tool — it would answer
**from thin air with no tool call at all**. This is the exact bug the capability catalogue exists to prevent.

### TC-B2 — Routing by **DESCRIPTION** (both agents are toolless)
**Goal to paste:**
> `Here is a customer review: "Shipping was fast but the product broke in two days." Analyse its sentiment and themes.`

**Expect:** routes to **`text-analyst`**, not `brief-writer`.
**Why it matters:** both show `tools: NONE`, so the tool list cannot distinguish them. **Only the description can.**
If this passes, description-based routing is genuinely working.

### TC-B3 — The reverse, to prove B2 wasn't luck
**Goal to paste:**
> `Take these findings and write a 150-word brief: React has 220k stars and 1,100 open issues.`

**Expect:** routes to **`brief-writer`**, not `text-analyst`.

### TC-B4 — No agent can do it → say so, don't fabricate
**Goal to paste:**
> `What is the current share price of NVIDIA right now?`

**Expect:** a **single `orchestrator` step** whose text says plainly that **no sub-agent has the required
capability**, answering from general knowledge only (or declining).
**FAIL if:** it hands this to `repo-analyst`/`wiki-researcher`, which then **invents a price**. That is the
worst failure mode in the system — a confident, tool-less fabrication.

### TC-B5 — Undescribed agent is flagged, not silently guessed at
**Do:** blank one agent's description (Edit → clear → Save) → run any goal.
**Expect:** the planner sees `use when: UNSPECIFIED — no description was configured` and should **avoid**
routing to it. Restore the description afterwards.

---

## C. No delegation (the default)

### TC-C1 — A trivial goal must NOT fan out
**Goal to paste:**
> `What is 2 + 2?`

**Expect:** exactly **one** node, badged **`orchestrator · no delegation`**.
Header reads *"The orchestrator answered this itself — no delegation was needed."*
**FAIL if:** it spawns a researcher + a writer. Delegation is the **exception**, not the default; an
over-eager plan burns a token mint, a task row, its own LLM calls and a summarisation hop for nothing.

### TC-C2 — Obeys a NEGATION literally
**Goal to paste:**
> `Research the history of the Eiffel Tower and do NOT write a brief about it.`

**Expect:** a **research step only**. **No `brief-writer` node.**
**Why it matters:** the deleted keyword router substring-matched `"write"` inside `"do not write"` and
produced a brief-writing step anyway. This test is the regression guard for that whole class of bug.

### TC-C3 — Zero sub-agents still runs
**Do:** deactivate **all** sub-agents → run `Summarise the theory of relativity in 3 sentences.`
**Expect:** the run **completes**, answered by the orchestrator alone.
**FAIL if:** the run fails with `UNASSIGNED_NODE` / "No active sub-agents are configured" — that gate was
removed; the backend must not demand sub-agents exist before letting the model decide it needs none.

### TC-C4 — The "Use Sub-Agents" toggle OFF
**Do:** toggle off → goal `Delegate this to your sub-agents and research React.`
**Expect:** a **settings gate modal** ("Your prompt asks for sub-agents, but the toggle is off") offering
*Run Solo Anyway* / *Enable Sub-Agents & Run*.

---

## D. Execution flow — parallelism & sequencing

### TC-D1 — Independent work runs in PARALLEL (one wave)
**Goal to paste:**
> `Compare the React and Vue GitHub repositories by stars and open issues.`

**Expect:** `Wave 1/2` containing **2 nodes** with the badge **`2 in parallel`**, then `Wave 2/2` with the
synthesis step (`depends on: …`).
**Proves:** the planner declared the two lookups as independent (`depends_on: []`) and the driver ran them
concurrently. A flat list would have hidden this.

### TC-D2 — Dependent work runs in SEQUENCE (two waves)
**Goal to paste:**
> `Research the history of the Eiffel Tower, then write a 150-word brief from what you find.`

**Expect:** `Wave 1/2` = research (`wiki-researcher`), `Wave 2/2` = `brief-writer` showing
`depends on: <research node>`.
**Then:** expand the writer → its **Summary** should visibly build on the researcher's findings (the
upstream summary is threaded into its input — never the raw transcript).

### TC-D3 — Fan-out cap
**Goal to paste:**
> `Compare these 12 JavaScript frameworks in parallel: React, Vue, Angular, Svelte, Solid, Preact, Lit, Alpine, Ember, Backbone, Knockout, Mithril.`

**Expect:** the run still succeeds. The planner is told the limits (**max 8 in parallel, max 5 deep**), so it
should plan within them; if it overshoots, the plan is rejected and **it re-plans itself** (see TC-F4).
**FAIL if:** the run 500s, or silently drops frameworks with no indication.

---

## E. Tool visibility — the execution tree (this is what you reported)

### TC-E1 — Tool calls are visible WITHOUT expanding
**Do:** run **TC-B1**.
**Expect:** on the collapsed node row, a **`tools:`** chip row naming the actual tool (e.g. `tool-github-stats-…`).
Green chip = success, **red = the call failed**.
**FAIL if:** you have to click Details to discover a tool was used at all — that was the old behaviour.

### TC-E2 — Tools appear LIVE, while the agent is still working
**Do:** run **TC-D1** and **watch without touching anything**.
**Expect:** node goes `running` → `working…` → **tool chips appear as the calls land** → `completed`.
**Why this used to be broken:** the node's `task_id` was only stamped on **completion**, so a sub-agent's
tools were invisible for the entire time it was actually calling them.

### TC-E3 — Details = the sub-agent's full pipeline
**Do:** expand any completed node → **Details**.
**Expect:** the same timeline the single-agent **Task Runner** shows:
`Guardrail (Input) → LLM Call → Tool Call (with the tool's name) → Guardrail (Output)`,
each with duration + tokens. Plus **"Summary returned to the orchestrator"**.

### TC-E4 — Run stats add up
**Expect:** the **Tool calls** tile equals the total number of tool chips across all nodes;
**Tokens/Cost** are non-zero and include the orchestrator's **planning + synthesis** spend, not just the nodes'.

### TC-E5 — A toolless agent says so honestly
**Do:** expand a `brief-writer` node.
**Expect:** **no tool chips**, and its timeline shows LLM-only. It answered from the model — which is correct
for a writer, and must not be dressed up as a tool call.

### TC-E6 — A FAILED tool call is surfaced, not hidden
**Do:** remove the GitHub tool's access from `repo-analyst` (Tools/Access), then run **TC-B1**.
**Expect:** a **red** tool chip; expanding shows the `tool_call` step `failed` with an error like `TOOL_DENIED`.
**FAIL if:** the node reports success having quietly answered without the tool.

---

## F. Failure, budget, cancel, approval

### TC-F1 — Cost budget stops the run early
**Do:** goal = **TC-D1**, set **Cost budget (USD)** = `0.0001` → Run.
**Expect:** run ends **`failed`** with **`BUDGET_EXCEEDED`**, and the error banner shows the spend.
Nodes already finished keep their results; nothing is left hanging.

### TC-F2 — Cancel mid-run
**Do:** start **TC-D2** → hit **Cancel** while a node is `running`.
**Expect:** run goes **`cancelled`**; the in-flight sub-agent is **torn down** (its pipeline aborts); no node
is left `pending`/`running`.

### TC-F3 — Deactivating an agent mid-run does **not** rewrite the running plan
**Do:** start a long run (**TC-D2**), and while it is running, deactivate the agent it is using. Let it finish,
then run the **same goal again**.
**Expect:**
- **Run 1 is unaffected** and completes. The roster is read **once, at plan time**, and the plan is fixed —
  the engine must not re-route a run out from under itself mid-flight.
- **Run 2 must not route to that agent** — it is gone from the catalogue.

**Red flag:** if Run 1 dies with a token-mint error, Auth is refusing to mint for the deactivated sub-agent —
that is an Auth/xAgent deactivation-ordering issue, not a routing one. Note it and report it separately.

### TC-F4 — The planner repairs its own bad plan
**Hard to force by hand** — the planner rarely names a non-existent agent. Watch for it in the logs:
`orchestration_plan_rejected` → it re-plans **once** with the reason, and the run proceeds normally.
If it fails twice, the run ends **`ORCHESTRATION_FAILED`** with a message naming the bogus target and the
valid ones. **The backend never substitutes an agent of its own choosing.**

### TC-F5 — HIL approval gate (`/hil`)
**Do:** set the orchestrator's HIL mode to **ask** → run **TC-D2**.
**Expect:** the run pauses at **`awaiting_approval`**; **Pending Approvals** appears on the run page; approve →
it proceeds; deny → the node fails per its `on_error` policy.

### TC-F6 — HIL does NOT gate a non-delegating run
**Do:** with HIL still on **ask**, run **TC-C1** (`What is 2 + 2?`).
**Expect:** it completes **without** an approval prompt.
**Why:** the gate approves *sub-agent creation*. A step the orchestrator runs itself creates no sub-agent —
prompting there would be asking permission for something that isn't happening.

---

## G. Security & isolation

### TC-G1 — Blocked content is never leaked into the tree
**Do:** run a goal that trips a guardrail (e.g. include a fake credit-card number).
**Expect:** the node shows the guardrail step and its **status** — but the tree **never renders the matched
content**. Only `tool_call` steps expose detail (tool name / version / call-id / error); every other step's
payload stays server-side.

### TC-G2 — A sub-agent cannot spawn sub-agents
**Do:** log in as a **sub-agent** identity → `/orchestrator`.
**Expect:** **403** / locked out. Delegation depth is capped at **1**.

### TC-G3 — Cross-tenant invisibility
**Do:** take a `workflow_id` from tenant A → open it while logged into tenant B.
**Expect:** **404** (not 403). Existence must never leak across tenants.

### TC-G4 — The sub-agent runs under its OWN identity
**Do:** expand a node → note its `task_id` → open `/tasks/{task_id}`.
**Expect:** the task's **agent is the SUB-AGENT**, not the orchestrator. Its tool/model access is confined to
**that** sub-agent's grants — which is why TC-E6 (denied tool) fails the way it does.

---

## H. Quick regression sweep (run after any orchestration change)

| # | Goal | Must see |
|---|---|---|
| 1 | `What is 2 + 2?` | 1 node, `orchestrator · no delegation` |
| 2 | `How many open issues does facebook/react have on GitHub?` | `repo-analyst` + a **GitHub tool chip** |
| 3 | `Analyse the sentiment of: "It broke in two days."` | `text-analyst` (**not** `brief-writer`) |
| 4 | `Compare React and Vue by stars and open issues.` | `2 in parallel`, then a synthesis wave |
| 5 | `Research the Eiffel Tower and do NOT write a brief.` | research only — **no writer node** |
| 6 | `What is NVIDIA's share price right now?` | orchestrator says no agent can do it — **no fabricated number** |
| 7 | Any of the above, budget `0.0001` | `BUDGET_EXCEEDED` |

---

## What to log when something fails

For any failing case, capture:
1. **The plan** — `GET /bff/api/xagent/v1/orchestrations/{id}` → `decomposition` (`llm` vs `template`) and
   `subtask_dag.nodes[].preset` (**which agent the planner actually chose**).
2. **The node's task** — `GET /bff/api/xagent/v1/tasks/{task_id}` → `task_steps` (did a `tool_call` step exist at all?).
3. **The workflow error** — `error_code` + `error_msg`.

That triple distinguishes the three very different failures:
**the planner chose wrong** (routing) · **the agent couldn't act** (tools/scopes) · **the UI didn't show it** (rendering).
