# Sub-Agent Setup + Test Ladder (new design)

> Your plan, reconstructed against what the code **actually does**. Four things in the original needed
> correcting — they're called out inline so you don't lose an afternoon to them.

---

## ⚠️ Corrections to your plan (read these first)

### 1. `mem:write` was **not** the 403 cause

There are **two different 403s** and they got merged:

| | Where | Whose scopes | Fixed by |
|---|---|---|---|
| **403 #1** — made the table say `No runtime` | `GET/PUT /v1/agents/{id}/runtime` | **the caller's session** (`_require_admin` → needs `agent:admin` or `platform:admin`) | your **session's** scopes |
| **403 #2** — silent, once per task | MEMORY_WRITE stage | **the sub-agent's own** scopes (`memory_scope != 'none'` + no `mem:write` = guaranteed 403 every task) | granting the sub-agent `mem:write` |

A sub-agent's `mem:write` **cannot** affect #1 — the runtime endpoint checks *your session*, not the target agent.
So: **still grant `mem:write`** (it genuinely fixes #2), but if the table still says `No runtime` afterwards,
your session is missing `agent:admin`. The page now says `Can't check — no scope` instead of lying about it.

> The UI now derives `memory_scope` from whether `mem:write` was granted, so #2 is **unrepresentable** going forward.

### 2. You cannot **delete** a sub-agent

There is no delete endpoint — only `deactivateSubAgent`. **Edit the four you have instead.** The Edit modal
now registers a missing runtime row, so it fixes `No runtime` and `Not described` in one action. That's
strictly less work than delete-and-recreate, which isn't even possible.

### 3. You gave system prompts, but the planner routes on **`description`**

These are **two different fields for two different readers**, and this is the core of the new design:

- **`description`** = *"when to use this agent"* — written **for the orchestrator's planner**. This is the
  **only** thing (besides its tools) that decides whether a step is routed here.
- **`system_prompt`** = the agent's **own instructions**, once a step has already arrived.

Your four prompts are good *system prompts*. They are **not** descriptions. Both are given below.

### 4. Attaching a tool takes **two** writes

`allowed_tools` on the runtime **and** a tool-registry **access grant**. An agent listed against a tool it was
never granted gets **`TOOL_DENIED`** at run time. The **Agent Builder** (`/agents/{id}`) does both — the
sub-agent modal does not. Your "open it in Agents → attach the tool → Save" step is therefore **required**,
not optional. (There's now an **Attach tools →** link in each agent's **Details**.)

---

## Setup — edit the four you have

For **each** agent: **Scopes** → add `mem:write` · **Edit** → paste the description + system prompt + model ·
**Details → Attach tools →** → attach the tool → Save.

First check `/tools` that these three tool names exist in your tenant. If the suffix isn't `cabd9d62`, use whatever is listed.

### `wiki-researcher` — model `smart`
**Scopes:** `agent:execute` `llm:invoke` `guardrails:check` `tool:invoke` `mem:read` `mem:write`
**Tool:** `tool-wikipedia-summary-cabd9d62`

**When to use (description):**
> Looks up background and historical facts on Wikipedia — people, places, projects, events, definitions. Returns a compact factual findings brief with citations. Cannot fetch live data such as GitHub statistics, prices, or anything not in Wikipedia.

**System prompt:**
> You research topics using the Wikipedia tool. Return a compact factual findings brief. Do not write the final prose answer.

---

### `repo-analyst` — model `smart`
**Scopes:** same as above
**Tool:** `tool-github-repo-info-cabd9d62`

**When to use (description):**
> Fetches live GitHub repository statistics: stars, forks, primary language, open issues, and licence. Use for any question about a GitHub repo's current numbers. Cannot answer general history or background questions.

**System prompt:**
> You look up GitHub repository statistics using the GitHub tool. Report stars, forks, primary language, open issues, and license as structured facts.

---

### `brief-writer` — model `smart`
**Scopes:** `agent:execute` `llm:invoke` `guardrails:check` `mem:read` `mem:write` *(no `tool:invoke` — it has no tools)*
**Tool:** none

**When to use (description):**
> Turns findings produced by other agents into a clear, well-organised written answer. Use as the final step when research has already been gathered. Performs no lookups of its own and must never be given a step that requires fetching data.

**System prompt:**
> You receive findings from other agents as context. Write a clear, well-organized answer grounded strictly in those findings. Never invent facts.

---

### `text-analyst` — model `fast`
**Scopes:** `agent:execute` `llm:invoke` `guardrails:check` `tool:invoke` `mem:read` `mem:write`
**Tool:** `tool-text-analyzer-cabd9d62`

**When to use (description):**
> Measures a piece of text that is given to it: word count, sentence count, reading time. Use when the goal asks for metrics ABOUT some text. It does not research, and it does not write prose.

**System prompt:**
> You measure text using the text analyzer tool. Report word count, sentence count, and reading time.

---

**✅ Setup is done when all four show `Schedulable: 🟢 Ready`, a real sentence under `When to use`, and the right chip under `Tools`.**
Open **Details** on each — the top block is *literally what the planner reads*:

```
- repo-analyst
    use when: Fetches live GitHub repository statistics: stars, forks, primary language...
    tools: tool-github-repo-info-cabd9d62
```

If that block wouldn't convince *you* to route a GitHub question here, it won't convince the planner either.

---

## The test ladder

### Test 1 — No delegation
> `What is 17 multiplied by 23?`

**Expect:** 1 node, badged **`orchestrator · no delegation`**. Zero sub-agents. Header: *"The orchestrator answered this itself."*
**Proves:** delegation is the *exception*. The old roster-free prompt demanded "2–5 steps", so the planner was
structurally incapable of saying "one agent is enough" and invented work to fill the quota.

### Test 2 — One specialist + tool visibility
> `Get the GitHub stats for the repository facebook/react.`

**Expect:** 1 node → **`repo-analyst`**, with a green **`tool-github-repo-info-…`** chip **visible without expanding**.
**No `brief-writer` node.** Expand → the full timeline (`Guardrail → LLM → Tool Call → Guardrail`).
**FAIL if:** it goes to `wiki-researcher` — that agent holds only the Wikipedia tool and would **fabricate the numbers with no tool call at all**.

### Test 3 — Parallel fan-out ⭐
> `Give me a briefing on the Linux kernel: its history and background from Wikipedia, and the current GitHub statistics for torvalds/linux. Then write a combined summary.`

**Expect a diamond:**
```
Wave 1/2  [2 in parallel] ── wiki-researcher (wikipedia tool)
                          └─ repo-analyst    (github tool)
Wave 2/2  ───────────────── brief-writer     depends on: both
```
**Proves:** the planner declared the two lookups independent (`depends_on: []`), the driver ran them
**concurrently**, and the writer fanned them back in. Watch the tool chips appear **live**, one per branch.

### Test 4 — Negation control 🔴
> `Research the history of the Linux kernel from Wikipedia. Do not write a brief or summary — just give me the raw findings.`

**Expect:** 1 node → `wiki-researcher`. **No `brief-writer` node.**
**Why this is the regression test:** the deleted keyword router substring-matched `write` inside `do not write`
and emitted a brief-writing step regardless. If a writer node appears, the fix did not hold — **tell me**.

### Test 5 — Sequential chain (multi-hop context)
> `Look up the Eiffel Tower on Wikipedia, write a short brief about it, then analyze that brief's word count and reading time.`

**Expect a 3-node chain across 3 waves:** `wiki-researcher` → `brief-writer` → `text-analyst`, each
`depends on:` the last.
**The real proof:** expand `text-analyst` → its **tool call** should report a word count matching
**`brief-writer`'s actual draft**, not the original one-line goal. A downstream node receives
`goal + "Context from prior steps:" + <upstream summaries>`, and a sub-agent's *summary is its final answer* —
so the writer's whole brief is what reaches the analyst.

---

## Tests your ladder is missing (each proves something none of the five do)

### Test 6 — Capability gap: refuse, don't fabricate 🔴 *(the most dangerous failure)*
> `What is NVIDIA's current share price?`

**Expect:** a single **`orchestrator`** step stating plainly that **no sub-agent has the required capability**,
answering from general knowledge only.
**FAIL if:** it routes to `repo-analyst`/`wiki-researcher`, which then **invents a price**. A confident,
tool-less fabrication is the worst thing this system can do.

### Test 7 — Description-only routing *(needs a 5th agent)*
Your roster can't test this: every agent is separable by **tools** alone. To prove **descriptions** are doing
real work, add one toolless agent:

**`fact-checker`** — no tools — description:
> Critiques an existing draft for factual errors, unsupported claims, and tone. Does not write new prose and does not look anything up.

Now `brief-writer` and `fact-checker` **both** render `tools: NONE` — the tool list cannot tell them apart, so
**only the description can**. Then run:
> `Here is a draft: "The Linux kernel was created by Linus Torvalds in 1991 and is written in Rust." Check it for factual errors.`

**Expect:** → `fact-checker`, **not** `brief-writer`.

### Test 8 — Budget ceiling
Run **Test 3** with **Cost budget = `0.0001`**. **Expect:** run ends `failed` / **`BUDGET_EXCEEDED`**; finished nodes keep their results.

### Test 9 — Cancel mid-run
Start **Test 5**, hit **Cancel** while a node is `running`. **Expect:** `cancelled`; the in-flight sub-agent is torn down; nothing left `pending`/`running`.

### Test 10 — Denied tool surfaces, doesn't hide
Revoke `repo-analyst`'s **access grant** for the GitHub tool (Agent Builder), leave `allowed_tools` set. Re-run **Test 2**.
**Expect:** a **red** tool chip; the `tool_call` step shows `failed` / `TOOL_DENIED`.
**FAIL if:** the node reports success having quietly answered without the tool. *(This is exactly the two-writes trap from correction #4.)*

### Test 11 — Zero sub-agents still runs
Deactivate **all four** → `Summarise the theory of relativity in 3 sentences.`
**Expect:** completes, answered by the orchestrator alone.
**FAIL if:** `UNASSIGNED_NODE` / "No active sub-agents are configured" — that gate was removed.

### Test 12 — Undescribed agent is avoided, not guessed at
Blank `repo-analyst`'s description → run **Test 2**.
**Expect:** the planner sees `use when: UNSPECIFIED` and should **avoid** it (likely answering via `orchestrator`).
**Restore the description afterwards.**

---

## If a test fails, capture these three things

They separate the only three failures that matter — and from the screen they look identical:

| Pull | From | Tells you |
|---|---|---|
| `subtask_dag.nodes[].preset` | `GET /bff/api/xagent/v1/orchestrations/{id}` | **which agent the planner chose** → routing bug |
| `task_steps` (any `tool_call`?) | `GET /bff/api/xagent/v1/tasks/{task_id}` | **whether the agent could act** → tools/scopes bug |
| `error_code` + `error_msg` | same run | how it died |

*The planner chose wrong* ≠ *the agent couldn't act* ≠ *the UI didn't show it.*
