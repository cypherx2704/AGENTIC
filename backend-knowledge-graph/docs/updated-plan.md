# Backend Knowledge Graph — Autonomous Backend Intelligence Platform (Final Definition)

> The definitive product + architecture statement. Readable companion to the deep implementation
> detail in [PLAN.md](PLAN.md) and the origin story in [base-idea.md](base-idea.md). Planning only —
> no code yet.

---

## 1) Vision

Build an **autonomous backend intelligence platform** that continuously **understands, validates,
documents, and tests** backend software throughout its lifecycle. The knowledge graph is *not* the
product by itself — it is the **persistent memory layer** that powers the product: an AI-native
backend intelligence system that both developers and coding agents reuse instead of re-deriving.

In plain terms: the platform **learns a backend once**, stores that understanding in a structured
local graph, and keeps it in sync as the code changes. In engineering terms: it is an incremental
project-intelligence layer that combines **deterministic program analysis**, **runtime observation**,
and **controlled AI semantic reasoning** to expose reliable backend facts — every one carrying its
own confidence and provenance — through an MCP server, a CLI, and a thin editor extension.

The knowledge graph is the durable engineering memory. Testing is the first capability built on it;
OpenAPI generation, docs, visualization, security analysis, breaking-change detection, and API
diffing are all *views* over the same graph.

---

## 2) The core problem

AI coding agents (Claude Code, Codex, Cursor) waste enormous token budgets **reconstructing the same
backend context from source on every prompt**. To "test the login endpoint," an agent re-derives the
routing chain (`app.use("/api", apiRouter)` → `userRouter` → controller → validation → DTO →
service), the auth rules, and sometimes the DB model — by reading many files one at a time. On a
medium codebase that is **30k–100k tokens per prompt**, and the cost compounds across every session,
every developer, every agent.

The problem is not that the AI can't reason. It is that the AI is forced to **re-derive
deterministic facts a machine can compute once and reuse many times**.

---

## 3) The solution

Build the project's structural understanding **once**, persist it in a local SQLite graph, and serve
it over MCP. Instead of reading hundreds of files, the agent asks for a structured endpoint summary
and gets the load-bearing facts in **~300 tokens**. The graph becomes the application's memory: it
tracks routes, controllers, schemas, middleware, auth, env vars, DB models, services, events, queues,
cron jobs, runtime behavior, and test surfaces — and AI assistants **query that shared memory** rather
than re-reading the codebase from scratch.

**Intended outcome:** a standalone, headless graph engine + thin VSCode extension + MCP server + CLI,
shipped as an open-core commercial product, that measurably cuts agent token usage on real test/debug
tasks while staying useful even to developers who never touch an AI assistant.

---

## 4) The prime directive — no LLM on the query path

**The MCP query path contains zero LLM calls.** Query time is a pure index lookup over a pre-built
SQLite database. All reasoning about the codebase happens **at build time**, using deterministic logic
and CPU cycles — never per query.

AI is still used, but only in a narrow, **build-time** role: optional bootstrap assistance for legacy
or untyped codebases, dynamic-route interpretation, and other ambiguous cases where static analysis
alone cannot confidently infer the truth. That makes **AI a helper, never the source of truth**, and
it is why the platform can *reduce* net token consumption even though it uses AI internally: the
expensive understanding happens once, offline, and every subsequent agent query is a cheap lookup.

This is the human-learning model made mechanical: an experienced engineer understands an application
once, keeps a persistent mental model, tracks incremental changes, updates only what moved, and
validates understanding through execution. The platform does exactly this — **understand once,
persist, incrementally update, continuously validate** — instead of re-reading the whole repo on
every question.

---

## 5) Is it feasible? Generic core + pluggable adapters

**Yes — a generic core with pluggable per-framework adapters is the correct design**, and it is how
mature tooling already works (LSP, tree-sitter, OpenTelemetry auto-instrumentation, OpenAPI
generators). HTTP routing across Express / Fastify / NestJS / FastAPI / Spring / ASP.NET / Gin all
reduces to the same primitives: *(route literal or annotation) + (handler symbol) + (prefix/mount
chain) + (validation schema ref) + (guard chain)*. Only **how those primitives are spelled** is
per-framework.

**Honest caveat.** The "90% generic / 10% adapter" framing is optimistic; the realistic split is
**70/30**, and the 30% concentrates in the high-value *imperative* case (Express, Gin):

| Leak | Why the universal model strains | Mitigation |
|---|---|---|
| **Schema inference without types** (Express, no Zod/Joi) | Body shape is the fixpoint of intra-procedural dataflow over `req.body`, not a fact in source | Lead with typed/validated endpoints; mark inferred shapes low-confidence; **let AI propose and runtime confirm** |
| **Dynamic routes** (`router[m](path)`, loops, config/DB-driven) | Const-folding + bounded unrolling fails on runtime-driven paths | Flag `dynamic:true` at low confidence, never drop; runtime/AI promote |
| **Auth semantics** (roles/permissions actually required) | Structurally uniform but semantically framework-specific and often not statically decidable | Capture the guard chain structurally; surface roles only at honest confidence |

**Design consequence:** confidence is **load-bearing, not cosmetic** (see §7), and the **core does all
resolution, confidence, assembly, sync, and serving** while adapters stay thin and file-local.

---

## 6) The hybrid, evidence-based intelligence model

The platform is **not a single static parser**. It is a multi-source inference engine that discovers
the application *by evidence* and records *how confident* it is in each fact. Every source writes into
the **same graph** through the **same confidence / provenance / merge model**.

There are four **deterministic / observational layers**, ordered by *how the evidence is acquired*:

| Layer | Evidence source | What it adds | Confidence ceiling |
|---|---|---|---|
| **L1 Static extraction** | source + framework adapters (tree-sitter) | routes, DTOs, schemas, models, middleware, env, services, events | `static-certain` for literals; `inferred` for derived |
| **L2 Call graph & data flow** | AST + types (ts-morph) | what an endpoint actually reaches through helper/service/repo indirection; outbound calls; coarse taint | `static-certain` only for typed 1:1 resolution; dynamic → `inferred` |
| **L3 Runtime enrichment** | optional preload agent | real routes / middleware order / response shapes / exceptions / outbound + DB calls observed live | `runtime-confirmed`; promotes or *conflicts* with L1/L2 |
| **L4 DB & config introspection** | live DB schema + multi-source config (opt-in, read-only) | DB-driven routes, feature flags, permission tables, live schema + drift | live observation → treated as a `runtime` source |

Then there is a fifth source that is **different in kind**:

| Source | Evidence | What it adds | Confidence ceiling |
|---|---|---|---|
| **L✱ AI semantic analysis** *(cross-cutting, not a sequential layer)* | AI Analysis Provider over a repo slice (bootstrap + incremental gap-fill) | architecture & module boundaries, conventions, framework/helper-abstraction patterns, indirect deps, business workflows, auth-flow narrative, service relationships | `ai-proposed` (promotable) / `ai-inferred` (terminal opinion); **never** `static-certain`, **never** self-promotes to `runtime-confirmed` |

**Why AI is a cross-cutting source and not "L5."** L1–L4 are ordered by how evidence is *acquired*
(source → dataflow → live process → external system). The AI layer **observes nothing** — it *reads a
slice and proposes*. It can propose a route L1 missed, a reachability edge L2 couldn't resolve, an
auth flow only runtime confirms, or a DB relationship in L4's domain. Pinning it to one pipeline slot
would either under-scope it or duplicate every layer. So it is quarantined **not by position but by a
structural confidence ceiling it can never exceed on its own**. (During a bootstrap it is often the
"understand-first" pass — an informal "L0" in narration — but it has no fixed slot in the resolution
pipeline.)

---

## 7) The confidence & provenance model (the rigor core)

Every node, edge, and field records **source**, **confidence**, **evidence/citations**, and
**verification status**. This is what turns the graph from *assumption-driven* into
*evidence-driven*, and it is the single most important part of the contract to get right.

**Provenance source:** `static | runtime | ai | merged`.
> `ai` is a deliberate, one-time addition to an otherwise-frozen enum. Introspection reused `runtime`
> because it genuinely *observes* a live system; AI observes nothing, so collapsing it into `static`
> or `runtime` would be a lie about provenance and would let an unverified guess inherit trust it
> never earned. AI is the one exception that earns its own source.

**Confidence tiers**, low → high:

```
ai-inferred  ≤  ai-proposed  <  inferred  <  { runtime-confirmed , static-certain }
                                                        ( conflict is orthogonal )
```

- `ai-inferred` — an AI **interpretation with no possible mechanical corroboration** (module
  responsibility, business-workflow prose, convention summary). **Terminal.** Rendered as *labeled
  opinion*, never a hard fact; **never gates CI or a diff**.
- `ai-proposed` — an AI-asserted graph fact that *could* be corroborated (an indirect dependency, an
  auth-flow step, an inferred body shape). **Promotable.** The best it can become via *static*
  corroboration is `inferred` — **never `static-certain`** (Golden Rule 1).
- `inferred` / `static-certain` / `runtime-confirmed` / `conflict` — unchanged from the deterministic
  model.

**Verification status** (drives AI promotion; for deterministic facts it is trivially set):
`unverified | static-corroborated | runtime-confirmed | developer-confirmed | conflict`.

| An AI proposal, then… | verificationStatus | resulting confidence | resulting source |
|---|---|---|---|
| nothing yet (corroboratable) | `unverified` | `ai-proposed` | `ai` |
| nothing yet (interpretation) | `unverified` | `ai-inferred` (terminal) | `ai` |
| static evidence agrees | `static-corroborated` | `inferred` *(never `static-certain`)* | `merged` |
| runtime observation agrees | `runtime-confirmed` | `runtime-confirmed` | `runtime` / `merged` |
| a developer confirms | `developer-confirmed` | confirmed band; **audit-logged + revocable** | `merged` |
| static or runtime disagrees | `conflict` | `conflict` (both facts retained) | (both) |

**The merger preserves the static ↔ runtime duality.** `static` and `runtime` remain the two
*observing* operands whose agreement yields `merged`. **AI enters as a one-directional third input:**
observation can corroborate or contradict an AI proposal (promoting it into the existing tiers, or
forcing `conflict`), but **AI never corroborates, promotes, or overwrites a static/runtime fact, and
never self-promotes.** A `static-certain` literal an AI merely *also mentioned* stays `static-certain,
source:static` — AI corroboration never taints a genuine fact's provenance.

### The two Golden Rules (they apply to AI verbatim)

1. **Existence ≠ reachability ≠ enforcement.** The syntactic presence of a call or auth scheme proves
   the *code exists*, not that it *runs on this endpoint* or *enforces anything*. Claims derived from
   presence-or-absence are `inferred` (or `ai-proposed`), **never `static-certain`**; only runtime
   promotes. `USES_SCHEME` (a credential mechanism is accepted) is separate from `VERIFIED_BY` (the
   token is actually validated). **AI may propose `USES_SCHEME`; it may never mint `VERIFIED_BY`** —
   that needs runtime. "Unauthenticated" is always phrased *"no enforcement visible in analyzed
   sources."*
2. **Never fabricate, never silently truncate.** Unresolved dispatch is recorded as *unresolved*, not
   invented as an edge. Multi-candidate resolution is an honest fan-out ("may touch one of {A,B}"),
   never a conjunction. **Any AI-proposed node/edge assembled into the graph requires a `file:line`
   code anchor**; a reasoning-trace-only assertion may live *only* as a terminal `ai-inferred`
   narrative, never as an assembled edge. When any budget/cap fires, the result carries `partial:true`.

**Structural enforcement, not honor-system.** The merger **refuses to emit any `static-certain` fact
whose certainty derives from an `ai` proposal** — exactly as the core already refuses to assemble an
`Endpoint` field an adapter didn't declare in `capabilities()`. If a served field with an `ai`
lineage ever renders `static-certain`, that is a bug.

**The `developer-confirmed` amendment.** Brief #2 adds human confirmation as an evidence source. This
is a *narrow, explicit amendment* to "only runtime promotes": a developer may promote an `ai-proposed`
fact into a **distinct confirmed band** — but that band **can never equal `static-certain`**, never
removes `ai` from the provenance chain, and every confirmation is **attributed, audit-logged, and
revocable**. It is the only human-authority override, and it is deliberately fenced.

---

## 8) AI repository analysis — the cross-cutting proposer

Static text, dataflow, runtime, and DB/config all *observe* the system. The AI Repository Analysis
Layer instead **proposes** the semantics deterministic analysis can't always decide — the biggest
lever on legacy/enterprise repos where a pure static bootstrap is slow, incomplete, and
framework-dependent. It reads a slice of the repo and asserts architecture, conventions, indirect
dependencies, helper abstractions, business workflows, the auth-flow narrative, and service
relationships. **The single governing rule: the AI is never the source of truth. It proposes; the
core merges its proposals at a capped confidence; only non-AI evidence (or a fenced human
confirmation) can promote them.**

**Initial bootstrap (legacy understanding).** During first indexing, static analyzers extract the
deterministic structure *and* the AI provider performs a full semantic pass; both merge into a
**baseline graph**. After bootstrap, there is **no expensive full-project AI pass** unless explicitly
requested.

**Incremental intelligence.** After bootstrap, the graph evolves incrementally. On a file / feature /
controller / schema / route / migration change, only the affected portion is recalculated. Static
analyzers handle deterministic updates; **AI is invoked only when (a) a deterministic layer emits an
`unresolved`/`inferred` hole in that domain, and (b) the input slice's content-hash changed.** Anything
unchanged replays from cache. Deterministic edits never wake an AI call. This is the cost model:
understand once, pin, replay for free, re-infer only the changed slice.

### Reconciling with the determinism oracle (the hard part)

The byte-identical-rebuild gate is **P0**; AI output is non-deterministic. Reconciled by making AI
facts **deterministic-by-pinning, not deterministic-by-recomputation**:

- **Every AI invocation is content-addressed.**
  `inputHash = H(canonical repo slice ⊕ prompt-template version ⊕ providerId ⊕ model ⊕ params ⊕
  provider-version)`. The result is stored as a **pinned, versioned artifact** at
  `.bkg/ai-cache/<inputHash>.json`. Given the same input, you **replay** the artifact — you do not
  re-invoke the model. (Slice construction must itself be **canonically, deterministically
  serialized** — deterministic traversal order, no wall-clock — or the hash is unstable. This is a
  **blocking** spec item, not an open question.)
- **The oracle splits, and both halves plus their sum are blocking:**
  1. **Deterministic-core gate** (unchanged P0): the incremental graph over L1/L2-static/L4-static is
     **byte-identical** to a from-scratch rebuild, *with AI facts excluded from this comparison* (they
     aren't recomputed).
  2. **AI-replay gate** (new): replaying the pinned artifact for an `inputHash` is byte-identical *by
     construction* (a cache hit, not an inference).
  3. **Merged-graph gate**: the fully merged graph (deterministic ⊕ replayed-AI) is byte-identical
     incremental-vs-rebuild.
- **Sealed replay mode.** Any deterministic context — the rebuild oracle, CI snapshot builds, cloud
  publish — runs the AI layer **replay-only**. **A cache miss in sealed mode is a fail-closed error,
  never a silent live model call.** Live `analyze` calls happen *only* in an explicit context
  (`bkg analyze --semantic`, or a dedicated bootstrap CI job) that then commits/publishes the pinned
  artifacts everything else replays.
- **Provider/model drift is visible.** Because `providerId + model + params + provider-version` fold
  into `inputHash`, changing the model forces an explicit re-analysis and a visible graph diff — the
  same repo state can never silently produce different facts.

### Team-canonical bootstrap (enterprise reproducibility & audit)

Single-lineage *replay* is already deterministic; only a *fresh* bootstrap on a different
provider/model/version legitimately differs. Enterprise resolves this by **canonicalizing the
bootstrap**: a team designates **one authoritative AI lineage**, built in a **CI bootstrap job with a
pinned provider + model + prompt-version**, and everyone **pulls and replays those pinned artifacts**
(via the cloud snapshot / shared cache, §13) rather than each running an uncontrolled fresh pass.
Within a team there is therefore exactly **one** AI lineage, and it is reproducible. Every AI fact
carries its `providerId + model + provider-version` stamp, so any proposal is **auditable** ("proposed
by `claude-code@1`, model X, prompt v3"). Upgrading the model is an **explicit, reviewed operation** —
like a dependency bump — that produces a visible graph diff and updates the shared baseline for the
whole team; it is **never silent**, and determinism-critical contexts (CI gates, diffs) only ever run
against the pinned baseline under sealed replay.

### Verifiability principle

Wherever a proposal *can* be checked, it is checked before its confidence is promoted. **Corroboration
raises confidence** (static agreement → `inferred`; runtime agreement → `runtime-confirmed`);
**contradiction is a first-class `conflict`**, never a silent overwrite in either direction.
Uncorroboratable narratives stay `ai-inferred` and are surfaced *as opinion* — `explainContext` leads
with *"proposed by AI, unverified — corroborate before trusting."* **An AI guess trusted as a
validated fact is worse than reading the file.**

### How an agent (and the platform) must treat AI facts

The enterprise stance is **serve everything, trust nothing silently.** `ai-proposed`/`ai-inferred`
facts *are* served — on a legacy repo, a labeled lead beats nothing — but **every payload carries its
`confidence`, `verificationStatus`, and `file:line` citations**, and `explainContext` leads with the
caveat. Consumption is tiered and **policy-gated by a confidence floor** (§22):

- **Exploration / onboarding / navigation / test-scaffolding suggestions** — `ai-proposed` is allowed
  (human-in-the-loop, low stakes).
- **Any automated gate** (breaking-change diff, security verdict, CI pass/fail, an auto-applied code
  change) requires a hard floor of **`inferred` or higher**, and **security / breaking-change gates
  require `runtime-confirmed`**. `ai-inferred` is **non-gating by construction** and can never fail a
  build.
- **Mutating actions** an agent drives require corroboration or an explicit, fenced
  `developer-confirmed`.

An enterprise admin sets the floor per use-case (conservative defaults above). A **graph-trust report**
(`bkg trust`) surfaces what fraction of the graph is unverified vs corroborated vs runtime-confirmed,
so teams see the maturity of their understanding and know where adding a test or a runtime session
would promote the most facts. Agents are instructed to treat `ai-*` facts as **leads to verify, not
facts to act on.**

---

## 9) Specialized AI subagents

One monolithic "AI repository analyzer" is the wrong shape — repository understanding is not one skill,
it's eight, and a single prompt attempting all of them is less accurate and impossible to gate
honestly. The AI layer is therefore a **fixed roster of specialized subagents**, each with a narrow
domain, a declared confidence ceiling, and **one deterministic subsystem it enriches rather than
replaces.** They are the AI mirror of adapters: as adapters are thin where the framework is
declarative, subagents are invoked only where deterministic analysis is honestly undecidable.

**Load-bearing rule: if a proposed subagent has no deterministic home for its output — and no
substrate to enrich — it doesn't ship.**

| Subagent (`AiTask`) | Domain | Enriches | Proposes (all `source:ai`) | De-risks |
|---|---|---|---|---|
| **API** (`api`) | routes, request/response contracts, versioning | `Endpoint` assembly + Field IR; `@bkg/context` | `{open,partial}` body/response shapes for untyped Express (`ai-proposed`, runtime-confirmable); version families | **Risk #1 "schema inference is theater."** Turns the untyped-Express blank into a promotable, confirmable shape |
| **Authentication** (`auth`) | authN/authZ, permission/role flows | `@bkg/auth`; `getAuthFlow` | proposed roles/permissions, guard-chain semantics, `TokenFlow` ordering, `USES_SCHEME` candidates (`ai-proposed`) | the "auth semantics" leak — surfaced as proposals, **never** `VERIFIED_BY`, never verdicts |
| **Database** (`database`) | ORM models, migrations, relationships | L4 / `@bkg/introspect` | implicit FK relationships, migration intent, `sensitive` column candidates | incomplete ORM static fallback; confirmed/conflicted vs live `information_schema` in P4 |
| **Testing** (`testing`) | test generation + maintenance | `@bkg/testing` synth; Phase-7 flow synthesis | flow orderings, response→request field threading for ambiguous matches, negative seeds | flow-synthesis ambiguity in the sandbox suite |
| **Security** (`security`) | vulnerabilities, unsafe patterns | `@bkg/security` (SARIF) | *suspect* findings (injection sinks, unsafe deserialization, over-broad CORS) — `ai-proposed`, advisory | thin static rule coverage — but **advisory, never CI-gating alone** |
| **Documentation** (`documentation`) | architecture + API prose | `@bkg/docs` + FreshArchDocs | business-workflow narratives, module-responsibility summaries, runbook prose (`ai-inferred`) | rotting hand-docs. Lowest risk: docs are a derived view, so hallucination degrades prose, not the graph |
| **Architecture** (`architecture`) | modules, boundaries, system design | OnboardingTour + `@bkg/viz` | module/service groupings, indirect deps, helper-abstraction roles, subsystem boundaries | "explain this 300-file codebase" — semantic layering the static topology can't name |
| **Runtime Reconciliation** (`runtime-reconciliation`) | runtime-vs-graph triage | `ConfidenceMerger`; Phase-4 `RuntimeIngest` | *explanations* for a `conflict` + a ranked stale-side hypothesis — **never an auto-resolution** | conflict-triage load: proposes *why* static↔runtime disagree so a human/runtime resolves faster |

### One interface, not eight

Subagents are **`AiTask` specializations behind a single vendor-neutral interface** — the same 70/30
boundary as adapters: providers are stateless and emit proposals only; they never resolve, assemble,
store, or serialize.

```ts
type AiTask =
  | "architecture" | "api" | "database" | "auth"
  | "testing" | "security" | "documentation" | "runtime-reconciliation";

interface AiAnalysisProvider {
  id: string;                                     // "claude-code@1" | "codex@1" | "gemini-cli@1" | …
  model(): { provider: string; model: string; params: AiParams };  // folded into inputHash
  capabilities(): AiCapabilities;                 // HONEST: which tasks + HARD ceiling (never > ai-proposed for facts)
  analyze(task: AiTask, slice: RepoSlice, ctx): Promise<AiProposalSet>;  // PROPOSALS ONLY
}

// AiProposalSet IS a PartialGraph whose every node/edge is stamped source:"ai",
// confidence:"ai-proposed"|"ai-inferred", verificationStatus:"unverified",
// and carries file:line CITATIONS + inputHash + providerId so the core can attempt corroboration.
```

Two structural guarantees mirror the adapter model: the **core refuses any `ai` proposal above the
ceiling `capabilities()` declared** (structural, not honor-system), and subagents **read the
deterministic graph as context and emit only proposals** — assembly and merge stay in the core.

### Orchestration

- **Bootstrap fan-out.** The orchestrator runs independent domains in parallel (Architecture / API /
  Database / Documentation), then dependent ones (Auth needs API; Testing needs Auth+API; Security
  needs Database's `sensitive` candidates). Each is handed the deterministic graph so it *augments*
  rather than rediscovers.
- **Incremental, gap-triggered only.** A subagent wakes only on an unresolved/inferred hole in its
  domain *and* a changed input hash. This is what keeps "AI only when semantics can't be derived
  automatically" from silently becoming per-keystroke spend.
- **One merger, no bypass.** Every proposal flows into the same `ConfidenceMerger`. AI can *fill a
  gap* or *raise a conflict*; it can never overwrite a `static-certain`/`runtime-confirmed` fact or
  the local graph.
- **Security/Auth findings are suspects, not verdicts.** A finding sourced *only* from the Security or
  Auth agent is **severity-capped to advisory**, rendered distinctly, and **cannot gate CI** until
  corroborated by a deterministic signal (a `FLOWS_TO` taint path) or runtime.

---

## 10) AI strategy & model supply

AI is used **sparingly and at build time**: one-time bootstrap, then on-demand only when static
confidence falls below a threshold for a newly added untyped file or dynamic construct. **Token
budgets are capped** so AI never becomes a runaway cost center, and no subagent is ever on the
critical path of the token-savings bet (§20).

The `AiAnalysisProvider` abstraction also makes the platform **model-supply-agnostic**. The same task
interface routes to any of three back-ends:

| Mode | What it is | When |
|---|---|---|
| **BYOK** (default) | customer's own Anthropic / OpenAI / Gemini key, or local **Ollama** | Early strategy: avoids token reselling, cuts latency, keeps proprietary code under customer control |
| **Self-hosted / local** | a reasoning model the customer runs on-box | Sensitive repos, air-gapped, cost control |
| **Platform-owned models** | the platform's own **fine-tuned** models for bounded tasks (schema inference, route interpretation, conflict resolution, test synthesis, doc generation) | Long-term: where the platform's own models are cheaper/faster/more accurate, or reduce external dependency |

This is the vendor-independence guarantee (brief #10) *and* a business hedge: early versions rely on
BYOK; as the product matures, selected workloads shift to platform-owned models where that wins.
Provider quality differences are handled by **per-provider `capabilities()` ceilings** plus a
**provider conformance golden-test suite** — a weaker provider is capped lower and never trusted
beyond its declared ceiling.

---

## 11) The graph model (the contract — `@bkg/protocol`)

`@bkg/protocol` is the **single, frozen source of truth** for the node/edge vocabulary, the RPC
contract, and the zod schemas. It has zero dependencies; adapters, AI providers, and feature packages
all *import* it and never extend it.

Core nodes: `File`, `Symbol`, `Route`, `Controller`, `Handler`, `DTO`/`Schema`, `Field`, `Middleware`,
`AuthPolicy`, `Role`, `Permission`, `Service`, `DbModel`, `EnvVar`, `Event`, `Queue`, `CronJob`, plus
per-surface `Endpoint` (HTTP), `GraphQLField`, `GrpcMethod`, `WsChannel`. Later subsystems add
`CallSite`, `ExternalDependency`, auth/trust-boundary nodes, config/introspection nodes, and
sandbox-testing nodes — always additively.

The **`Endpoint`** is the denormalized "hero" payload the product exists to serve, **assembled by the
core** (never emitted by an adapter) from `Route + HANDLES + VALIDATES_WITH/RETURNS + GUARDED_BY`:

```ts
interface Endpoint extends BaseNode {
  kind: "Endpoint";
  method: HttpMethod; path: string;               // fully-resolved "/api/v1/users/:id"
  pathParams: Param[]; query: Param[]; headers: Param[];
  body?: SchemaRef;                               // → DTO node or inline; supports {partial:true, open:true}
  responses: { status: number|"default"; schema?: SchemaRef }[];
  auth: { required: boolean; policies: NodeId[]; roles: string[]; permissions: string[] };
  middlewareChain: NodeId[];                      // ordered, post nesting-resolution
  handler: { node: NodeId; file: string; line: number };
  emits?: NodeId[]; consumes?: NodeId[]; persistsTo?: NodeId[]; readsEnv?: NodeId[];
  confidence: Confidence;                          // incl. ai-proposed / ai-inferred
  source: "static" | "runtime" | "ai" | "merged";
  verificationStatus: VerificationStatus;
}
```

**Field IR** carries `{name, type, required, format, min, max, pattern, enum, items, ref, open?,
source, confidence}` where `source ∈ validation-lib | static-type | destructuring | runtime | ai |
unknown` — provenance is never collapsed (diffing and breaking-change detection need it).

**The two thin contracts (the whole per-plugin surface):**

```ts
interface Adapter {                               // per-framework, deterministic
  id: string; capabilities(): Capabilities;
  detect(project): Promise<DetectResult>;
  parseFile(uri, src, ctx): Promise<PartialGraph>;    // LOCAL facts + symbolRefs + RouterMounts
  resolveLocal?(graphs, ctx): PartialGraph;           // ONLY framework-specific nesting
}

interface AiAnalysisProvider { … }                // per-vendor, proposal-only (see §9)
```

Both are stateless and emit `PartialGraph` only. **Route nesting, symbol resolution, assembly,
confidence, merge, storage, and serialization are all the core's job.** If a plugin does any of them,
the boundary has leaked.

### The semantic overlay — AI-only concepts stay out of the frozen core

Purely semantic, AI-only concepts — `Module`, `Workflow`, `ArchLayer`, subsystem boundaries — do **not**
enter the frozen `@bkg/protocol` node vocabulary. They live in a **separately-versioned semantic
overlay** (`@bkg/protocol-semantic`) whose nodes are always `source:ai`, carry `ai-proposed` /
`ai-inferred` + citations, and **reference core node IDs via edges but are never referenced by
deterministic assembly.** This is the enterprise-correct separation: the deterministic contract that
adapters, diffing, security, and the license boundary depend on stays **frozen and pure**, while the
softer, faster-evolving semantic model versions on its own cadence. Deterministic-only views (CI gates,
breaking-change diff, security) query the core and **never see overlay opinion** unless explicitly
asked; onboarding / architecture / context views join the overlay in. The overlay is **non-gating by
construction.**

---

## 12) Runtime learning & DB/config introspection

**Runtime enrichment (L3)** is an optional preload agent (`node -r`) with `httpTap` / `eventTap` /
`dbTap` / `errTap`, reservoir sampling, and **shape-only redaction by default**, writing to a separate
disposable store. Runtime observations either **confirm** graph knowledge, **improve confidence**, or
surface a **`conflict`** — never a silent overwrite. This is what promotes dynamic routes, confirms
inferred shapes, detects response-schema drift, and turns `ai-proposed` facts into `runtime-confirmed`.

**DB & config introspection (L4)** is **opt-in and read-only**. It reads live DB schema (Postgres
`information_schema`, MySQL, SQLite `PRAGMA`, bounded Mongo sampling; ORM static fallback via Prisma
DMMF / TypeORM / SQLAlchemy / Sequelize), allowlisted registry/permission/flag tables, and config from
`.env`/YAML/JSON/TOML/Helm/k8s (values redacted). Live-schema-vs-ORM agreement → `runtime-confirmed`;
drift → `conflict` (a first-class migration-drift signal).

**Mandatory, non-negotiable safety (defense in depth — all required, not "preferred"):** a read-only
transaction **and** a `GRANT SELECT`-only role **and** a SELECT/SHOW/PRAGMA/catalog statement
allowlist **and** a hard statement timeout + row caps + single connection; refuse a prod-looking host
without a second confirm; DSN explicitly configured (**never auto-discovered**); credentials never
stored in graph/snapshot/telemetry; raw rows land in a disposable scratch store, only derived facts
persist. **No writes/DDL, ever.** Any control here marked "preferred" instead of enforced is a
ship-blocker.

---

## 13) Local-first graph + cloud team-approved snapshot

**Local graph = current working reality** — per-branch, continuously updated, fast, private, offline,
tolerant of broken/unmerged code, carrying all confidence levels including `conflict`/`inferred`/`ai-*`.

**Cloud graph = team-approved shared snapshot** — published *only* for stable states (merge to main,
release tag, CI-verified, or a reviewer-approved diff), so every teammate's agents query one consistent
map. **Never auto-push the working tree.** The authoritative snapshot is built **in CI** (the trust
anchor) via keyless **OIDC**; a **fail-closed redactor** strips secret values and publishes shape-only
runtime facts. For AI facts, the snapshot may carry the *derived* nodes/edges (+ `source:ai` +
confidence + `verificationStatus` + `file:line` citations) but **never** the prompt, the model's raw
reasoning, or source excerpts — pinned artifacts and repo slices stay local/CI. Pull loads a cloud
snapshot **read-only alongside** the local graph and merges via the same `ConfidenceMerger` — it can
confirm or surface conflict, **never overwrite** local. Divergence is *surfaced*, never auto-reconciled.
The **team-canonical AI baseline** (§8) rides the same channel: teammates **pull the CI-pinned AI
artifacts** and replay them rather than each running a fresh, divergent bootstrap — so the whole team
shares one reproducible, auditable AI lineage.

---

## 14) The testing engine

Because the graph already knows the backend's structure, the testing package **generates and executes
tests deterministically, with no runtime LLM calls** — the AI's ambiguous-match help is drawn from
pre-computed `ai-proposed` facts, not a live call.

**v1 suites (ship first):** smoke, happy-path, negative/validation, auth/authz matrix, contract
(response vs schema), data-integrity (assert DB state), security-smoke (unauth→401, IDOR). Flows are
synthesized from the graph — group CRUD endpoints by `DbModel` into `create→read→update→delete`
`TestFlow`s, prepend a login/token step, thread response→request fields (ambiguous matches flagged
low-confidence). The sandbox uses **`testcontainers`** with the **Ryuk reaper** for guaranteed
teardown, **fake secrets only**, default-deny network, and a tiered no-Docker fallback.

**Long-term catalogue (phased, deliberately NOT all at once — that is the classic scope-killer):**
unit, integration, API, E2E, contract, regression, smoke, security, performance, load, stress, chaos,
mutation, boundary, validation, authorization, DB-consistency, event-driven, queue-processing, and
microservice-interaction testing — using standard drivers (Vitest, k6, Pact, Testcontainers). Each
category earns its place *after* the graph facts it depends on exist; load/soak, fuzzing, and
idempotency are explicitly deferred.

---

## 15) Generated artifacts

The same graph produces developer artifacts, so the product stays valuable to developers who never use
an agent: **OpenAPI 3.1** (the canonical hub all exporters derive from), curl, Postman / Bruno /
Thunder / Insomnia / REST-Client `.http`, and markdown documentation.

---

## 16) Graph-native intelligence (the moat)

Features that are pure **traversal / taint / diff** over the same graph — a competitor without a graph
can't easily do them. The headline **agent-facing trio** ships together:

- **BlastRadius** — reverse closure: "what breaks if I change this DTO/model/endpoint?"
- **AgentGuardrail** — a pre-edit `preflight(file|symbol)` returning blast radius + the exact tests to
  run + untested callers, in ~300 tokens.
- **EndpointTestCoverage** — which endpoints have zero tests (needs a `COVERS` edge).

Plus **EnvVarForEndpoint**, **PIIDataMap** (taint from `sensitive`-tagged columns to response
surfaces), **N1Detector**, **OrphanHunter** (human-gated), **MigrationGuard**, **IncidentAssist**, and
**OnboardingTour**. New schema across the whole backlog is tiny: a `COVERS` edge and a `sensitive` tag.

---

## 17) Product architecture & monorepo

Four replaceable layers: the **VSCode extension** (thin — watches files, shows UI, supervises the
daemon; *no analysis logic*), the **analysis engine** (`@bkg/core` — extraction, resolution, merge,
query), the **knowledge store** (SQLite), and the **MCP / tool server** (serves the graph, launches
tests). The CLI consumes the same RPC surface as the extension — which is the proof the extension is
genuinely thin.

```
backend-knowledge-graph/
├─ packages/
│  ├─ protocol/     @bkg/protocol   Node/edge model + RPC + zod. Zero deps. FROZEN SOURCE OF TRUTH.
│  ├─ core/         @bkg/core       Engine: GraphStore abstraction, symbol/nesting/assembler/ConfidenceMerger, sync-engine, query, Event Bus, Task Scheduler, Capability Registry. Headless.
│  ├─ daemon/       @bkg/daemon     Long-lived per-workspace process; owns the GraphStore (single writer), watcher, worker pool + scheduler.
│  ├─ adapter-sdk/  @bkg/adapter-sdk  Adapter base + ParseContext + sidecar harness.
│  ├─ adapter-express / adapter-nest / adapter-fastify / adapter-fastapi   Framework adapters.
│  ├─ ai/           @bkg/ai         AiAnalysisProvider abstraction + Provider Registry + task-based subagent orchestrator + pin/replay cache. Providers import protocol only.
│  ├─ mcp/          @bkg/mcp        Thin MCP server (stdio + Streamable HTTP). Proxies core; never reads SQLite. NO LLM on the query path.
│  ├─ runtime/      @bkg/runtime    Optional preload agent → RuntimeFact.
│  ├─ testing/      @bkg/testing    runner (undici) + synth (faker) + emit (OpenAPI hub + collections) + orchestrate.
│  ├─ openapi/      @bkg/openapi    (snapshot)⇒OpenAPI 3.1 — the artifact hub.
│  ├─ docs / viz / security / diff / review / context   Feature packages, each a pure (snapshot,opts)⇒artifact.
│  ├─ cli/          @bkg/cli        Headless `bkg …`. CI gate entry point.
│  └─ sdk/          @bkg/sdk        Library client + asTools() for raw agent loops.
├─ apps/vscode-extension/   Thin supervisor + watcher + TreeView/CodeLens/StatusBar/Cytoscape webview + MCP auto-register.
├─ sidecars/                Roslyn / JavaParser / go-packages probes (later, NOT bundled in MVP).
└─ fixtures/                Reference apps per framework for E2E/golden tests.
```

**Core engine building blocks (the scalability spine).** Inside `@bkg/core`: a **`GraphStore`
abstraction** (SQLite first, swappable for an enterprise backend without changing the content-addressed
snapshot contract); an internal, typed, **in-process Event Bus** decoupling the pipeline
(watch → extract → merge → invalidate → serve); a **Task Scheduler** + Piscina worker pool that runs
only *non-deterministic* enrichment (runtime, AI, introspection) **off the hot path**; and a
**Capability / Plugin Registry** where adapters, AI providers, and sidecars register with the honest
`capabilities()` + confidence-ceiling declaration the merger enforces. **Determinism guardrail:** the
deterministic extraction/merge path has fixed, storage-independent ordering — the Event Bus and
Scheduler never introduce ordering nondeterminism, so the byte-identical rebuild oracle (§20) still
holds. **Anti-premature-abstraction guardrail:** the MVP ships **only** the SQLite `GraphStore`; a
second backend is built only when a measured scale requirement demands it — the abstraction is the
seam, not a mandate to implement two stores.

**Dependency rule:** `protocol` ← `core` ← {daemon, mcp, cli, sdk, feature pkgs}; **adapters and AI
providers depend only on their SDK + `protocol`**; `apps/*` depend only on `protocol` + `sdk`. **The
package boundary IS the license boundary** — paid features (`diff`, `security`, `review`, provider
connectors) are separate packages, so the OSS core stays clean and headless-runnable without a license.

---

## 18) Tech stack & scalability spine

Several rows below are **architectural components**, not just library picks — the load-bearing spine
that lets the platform scale without rewrites. Guardrails on the new ones are noted in §17.

| Concern | Choice |
|---|---|
| Language / runtime | TypeScript, Node ≥20, ESM, strict |
| Monorepo | pnpm workspaces + Turborepo + **Changesets** (versioned multi-package release); boundaries via `eslint-plugin-boundaries` |
| Structure parser (all langs) | `web-tree-sitter` + official grammars (ts/tsx/py/java/c#/go) |
| Semantic analysis (TS) | `ts-morph` (`getTypeChecker`) |
| Non-JS analysis | **RPC-based sidecar adapters** — Roslyn (C#) · JavaParser (Java) · LibCST (Python) · go/packages (Go); out-of-process, user-installed, **not bundled in the MVP** |
| Storage | **`GraphStore` abstraction**, SQLite (`better-sqlite3`, WAL) as the first implementation; pluggable so an enterprise backend can swap in later **without changing the snapshot contract** |
| Diffable artifact | canonical, **storage-independent** `.bkg/snapshot.json` (content-addressed); `.bkg/graph.db` + `.bkg/ai-cache/` gitignored |
| Background jobs | **Piscina worker pool + Task Scheduler** (off-hot-path enrichment: runtime, AI, introspection); chokidar / VSCode FileSystemWatcher for file watch |
| Event system | internal, typed, **in-process Event Bus** (watch → extract → merge → invalidate → serve); **deterministic ordering on the extraction/merge path** |
| Plugin system | **Capability / Plugin Registry** — adapters, AI providers, sidecars, and feature packages register with an honest `capabilities()` + confidence-ceiling declaration the merger enforces |
| AI | **task-based AI Orchestrator + Provider Registry** (`AiTask` → provider): BYOK (Anthropic/OpenAI/Gemini) · local (Ollama/self-hosted) · platform-owned models. Provider CLIs user-configured, **never bundled in the VSIX** |
| MCP SDK | `@modelcontextprotocol/sdk` v1.x (stdio + Streamable HTTP) — **no LLM on the query path** |
| Runtime instrumentation | preload agent (`node -r`): httpTap / eventTap / dbTap / errTap |
| HTTP test client | `undici` |
| Test drivers | Vitest (unit/integration) · k6 (load) · Pact (contract) · Testcontainers (sandbox) · **Playwright (future — browser/E2E)**; `@faker-js/faker` seeded data |
| OpenAPI validate | `@redocly/openapi-core` |
| Visualization | Cytoscape.js (dagre/fcose) + Mermaid + DOT + **GraphML** export |
| Security output | SARIF |
| Build | esbuild + `@vscode/vsce`; golden snapshot tests |
| Distribution | VS Marketplace + Open VSX + **CLI**; **future Desktop App** (another thin client on the same headless core) |

---

## 19) Phased roadmap

Sizing: **S** ≤1 wk · **M** 2–4 wk · **L** 1–2 mo. **The deterministic MVP proves the bet before any
AI ships.**

- **Phase 0 — Skeleton & frozen contract (S).** Monorepo; `@bkg/protocol` — including the AI **schema
  slots** (`ai` source, `ai-proposed`/`ai-inferred` tiers, `verificationStatus` field) declared **but
  unused**; SQLite `GraphStore`; `Adapter` interface; fixtures; golden harness. *No merger AI logic,
  no cache, no provider code — only the frozen slots exist.* **Exit:** hand-authored `PartialGraph`
  round-trips → golden snapshot passes.
- **Phase 0.5 — AI bootstrap spike (optional, off the critical path).** An isolated research spike to
  validate the AI-layer bet on one legacy repo. Informs the AI milestone; **the MVP does not depend on
  it.**
- **Phase 1 — MVP: prove token-savings (L) ⭐.** `adapter-express` → core resolution → `@bkg/context`
  → in-process stdio `@bkg/mcp` (no daemon) → `@bkg/testing` runner → minimal `@bkg/cli` → the
  measurement harness. **Explicit NON-goal: the graph-build path uses NO AI.** If bootstrapping needed
  an LLM you'd spend the very tokens you claim to save. **Exit:** agent tests login in **≤500 tokens**
  vs a measured **30k+** baseline (**≥90% reduction**), same correct request; fully-joined path proves
  nesting; cross-file schema proves symbol resolution; edit `app.use` prefix → re-query in <150ms.
- **Phase 2 — Deterministic breadth + artifacts + full extension (M→L).** `adapter-nest/fastify/
  fastapi`; `@bkg/daemon` + incremental sync; testing synth + collections; `@bkg/docs`/`@bkg/viz`; full
  extension; the **L2 inter-procedural engine** + static auth model; the **per-developer local-testing
  graph** (`bkg test --changed`). *Kept deterministic — the AI subsystem is not stacked here.*
- **AI Semantic Layer — dedicated milestone (L), sequences after Phase 2.** The single home for
  `@bkg/ai`: the `AiAnalysisProvider` interface, the `AiProposalSet` merge path, the `ConfidenceMerger`
  generalization (AI as one-directional third input), the content-addressed **pin/replay cache**,
  **sealed-replay mode**, the **provider conformance golden suite**, and the **first reference
  provider (Claude Code)**. AI bootstrap + gap-fill on typed frameworks. **No subagent ships before
  this substrate exists.** Subagents then layer in, one at a time, on the subsystem each enriches:
  Architecture/API/Documentation first (their substrates exist by P2), then Auth/Security/Database with
  Phase 3–4, Testing with Phase 7, Runtime-Reconciliation with Phase 4.
- **Phase 3 — Commercial / CI tier (L) — paid.** `@bkg/diff` ⭐ (graph breaking-change classifier),
  `@bkg/security` (SARIF), dead-endpoint *suspect*, `@bkg/review` PR bot, the graph-native trio,
  licensing. AI Security/Auth agents feed *advisory* findings here.
- **Phase 4 — Runtime observation + reconciliation (L).** `@bkg/runtime` taps; static↔runtime merge;
  runtime-confirmed dead-endpoints/drift/dynamic-route promotion; live DB/config introspection; the
  **Runtime-Reconciliation agent** triages conflicts; `ai-proposed` facts get promoted or conflicted.
- **Phase 5 — Scale & enterprise (M).** Sidecar adapters (Spring/ASP.NET/Gin) as user-installed
  plugins; analytics; enterprise (SSO, on-prem, policy gates, audit); the **confidence-floor policy
  engine**, the **`bkg trust` graph-maturity report**, and the **team-canonical AI baseline** with
  reviewed model upgrades.
- **Phase 6 — Team sync & cloud graph (M→L).** Cloud publish/pull; multi-ref/multi-provider (GitHub +
  GitLab) keyless CI; divergence UI; snapshot-determinism CI gate (blocking).
- **Phase 7 — Sandbox autonomous testing (L).** Ephemeral testcontainers sandbox; graph-driven flow
  synthesis; v1 multi-type suites; guaranteed teardown. The **Testing agent** fills ambiguous matches
  from `ai-proposed` facts.

---

## 20) What to build first + verification

**Build first (load-bearing):** `@bkg/protocol` + `@bkg/core` GraphStore + the resolution pipeline
(symbol → nesting → assembler → merger), driven by a **hand-authored `PartialGraph`** and a golden
snapshot test — *before any real parser exists*. The model + resolution are the moat; parsing is the
easy-to-replace part. Then: Express Tier-1 routes → nesting + symbol resolution → Tier-2 schema
extraction → thin MCP server → test runner + `runEndpoint` → the measurement harness.

**The token-savings proof (`scripts/demo-tokensave.ts`).** Two arms, metered identically with the SDK
`usage` tokens (ground truth): **Arm A** (filesystem read/grep + HTTP) vs **Arm B** (`bkg` MCP:
`explainContext`/`getEndpoint`/`runEndpoint`), same prompt ("test the login endpoint"). **Success:**
Arm A 30k–60k tokens; Arm B **≤500** tool I/O; **reduction ≥90%**; identical resolved request; correct
joined path + cross-file schema *without the agent reading any file*. **Falsifiable — if Arm B can't
beat 90% with a correct request, the central bet fails; stop.**

**The determinism oracle (from the first sync sprint).** A CI test applies a random edit sequence, runs
incremental update, and asserts the merged graph is **byte-identical** to a from-scratch rebuild
(§8's three gates: deterministic-core, AI-replay, merged). Any divergence is P0.

**The AI-cost-recovery metric (when the AI layer lands).** Meter bootstrap token spend vs downstream
per-task savings and report the payback point. **Bootstrap cost must be recovered within N downstream
agent tasks** — if not, re-scope where AI is invoked. A per-save edit that re-triggers a full-repo AI
pass means you're in the trap.

---

## 21) Risks & mitigations

| # | Risk | Mitigation | Early signal |
|---|---|---|---|
| 1 | **Schema inference is theater** for untyped Express | Sell high-confidence extraction (typed) as flagship; untyped is "skeleton + verify"; AI proposes (`ai-proposed`), runtime confirms | 20-repo corpus week 1; untyped-Express body-shape accuracy <60% ⇒ fix positioning |
| 2 | **Token savings unproven** | The measurement harness is the week-1 deliverable, before features | One repo must already cut agent tokens on a real task |
| 3 | **Incremental-sync drift** — a wrong graph that looks authoritative | Cheap full-rebuild oracle; content-hash everything; reverse-dep invalidation | Oracle divergence rate > 0 ⇒ stop |
| 4 | **Scope kills shipping** | Ruthless Phase-1 MVP; no daemon/sidecars/webview/AI in the MVP | Month 2 with no real tokens saved ⇒ in the trap |
| 5 | **Polyglot sidecar tax** | No sidecar in MVP; TS-only P1–2; Java/C#/Go are post-PMF plugins | Anyone bundling `dotnet`/JVM in the VSIX = red flag |
| 6 | **"Existence = truth" fallacy** | Presence-derived reachability/enforcement is `inferred`, never `static-certain`; `USES_SCHEME` ≠ `VERIFIED_BY`; only runtime promotes | Any presence-derived finding rendering `static-certain` = a bug |
| 7 | **Call-graph over-claim / silent truncation** | `TOUCHES` = weakest-link confidence + `partial:true`; fan-out never conjunction | A `dynamic-unresolved` hop yielding `static-certain` |
| 8 | **Live DB introspection blast radius** | Mandatory read-only txn + SELECT-grant + allowlist + timeout + single conn; opt-in; prod-host refusal; shape-only | Any control marked "preferred" instead of enforced |
| 9 | **AuthManager mints real tokens** | v1 accepts a pre-minted test token; auto-minting is default-off, host-allowlisted, test-creds-only | A token-armed run against a non-allowlisted host must hard-refuse |
| 10 | **Stale local-testing graph → false-green** | Content-hash gate; "stale" badge; refuse `--changed` when the daemon is behind | `--changed` impacted set diverges from a full run |
| 11 | **OIDC claim-scoping bug publishes over prod** | `ref` claim must equal the target + allowed-publisher claim, fail-closed | A claim mismatch that is silently published |
| 12 | **Sandbox Docker unavailability / teardown leaks** | Tiered no-Docker fallback; Ryuk reaper + try/finally + orphan sweep; per-run caps | A crashed run leaves labelled containers behind |
| 13 | **AI as false authority** — a hallucinated relationship rendered as fact | AI is never the source of truth; enters at `ai-proposed`/`ai-inferred`, capped; promotion needs corroboration or is `conflict`; structural merger guard; `explainContext` leads with "proposed by AI — unverified" | Any `ai`-lineage fact rendering `static-certain`, or promoting without corroboration = a bug (same tripwire as #6) |
| 14 | **AI non-determinism breaks the byte-identical oracle** | Content-hash-pinned, versioned, replayable artifacts; **sealed replay mode — a cache miss in oracle/CI/publish is a fail-closed error, never a live call**; determinism gate scoped to deterministic layer + AI-replay | A rebuild that issues a live model call in sealed mode; a cache key omitting model/prompt version |
| 15 | **AI bootstrap cost is real and up-front** | Bootstrap once + gap-triggered incremental + aggressive caching; no full pass unless requested; **measured payback within N tasks** | Per-session cost scaling with repo size not changed-slice; a save re-triggering a full-repo pass |
| 16 | **Provider quality drift / lock-in** | Generic `AiAnalysisProvider` + per-provider `capabilities()` ceilings + a conformance golden suite | A provider's golden score below its declared ceiling; a fact trusted above the provider's capability |
| 17 | **AI layer + 8 subagents = large new surface (re-triggers #4)** | AI is **additive enrichment, never a prerequisite**; the Phase-1 MVP ships unchanged (AI-free); subagents land phased, one at a time, behind the provider interface | Any Phase-1 exit criterion depending on an AI provider; a subagent on the MVP critical path |

**Invariant-preservation guard (AI additions).** Every AI capability is bound by the same
non-negotiables as the rest of the engine — **enforced, not "preferred."** Golden Rule 1 holds for AI
(an AI-proposed auth flow is `ai-proposed`, never `static-certain`; AI never mints `VERIFIED_BY`).
Golden Rule 2 holds for AI (a hallucinated edge is worse than an `unresolved` one; assembled edges
require a `file:line` anchor; capped passes carry `partial:true`). The `ConfidenceMerger` never
overwrites — AI facts confirm, raise confidence, or surface `conflict`, and never auto-push to the
cloud. `@bkg/protocol` stays the single frozen owner: the AI layer adds exactly **one** provenance
source (`ai`), **two** confidence tiers (`ai-proposed`, `ai-inferred`), and **one** field
(`verificationStatus`) — no per-fact AI flags. AI-only *semantic* concepts (`Module`, `Workflow`,
`ArchLayer`) live in a **separately-versioned semantic overlay** (§11), never in the frozen core, so
the deterministic contract stays clean and CI/diff never gate on opinion. **Any AI-sourced fact that
renders `static-certain`, or any control here marked "preferred" instead of enforced, is a
ship-blocker.**

---

## 22) Business model

**Open-core.** Free OSS core (`@bkg/core`, adapters, MCP, OpenAPI generation, context delivery, test
execution, basic docs/viz) drives adoption and adapter contributions. Paid Team/CI tier = the
continuous/coordination features with real willingness-to-pay (`@bkg/diff` breaking-change ⭐,
`@bkg/security`, `@bkg/review` PR bot, runtime-confirmed dead-endpoints, snapshot history, hosted
dashboard, and platform-owned model access). Enterprise = SSO, on-prem, policy gates, audit.
**Tension to decide deliberately:** `@bkg/diff` is both the most demoable feature *and* the flagship
paid one — consider a generous free CI quota so it stays demoable.

**Enterprise trust controls (paid).** A **confidence-floor policy engine** lets an admin set, per
use-case, the minimum confidence the platform will act on (conservative defaults: automated gates ≥
`inferred`; security / breaking-change ≥ `runtime-confirmed`; `ai-inferred` never gates). The
**graph-trust report** (`bkg trust`) quantifies unverified-vs-corroborated coverage for audit and
maturity tracking, and pins the **team-canonical AI baseline** with reviewed, diffed model upgrades.
Reproducibility, auditability, and policy-gated confidence are the enterprise value layered on top of
the open core.

---

## 23) Final vision

The long-term objective is not merely to generate a knowledge graph or automate testing. It is to
build an **autonomous backend intelligence layer that continuously understands, validates, documents,
and tests software systems throughout their lifecycle.** By combining deterministic program analysis,
AI-driven semantic understanding, runtime verification, and continuous learning — each fact carrying
its own honest confidence and provenance — the platform becomes the **persistent engineering memory
for both developers and AI agents.** It moves repetitive reasoning off the LLM and onto local
deterministic computation, eliminating repeated repository analysis while enabling faster development,
higher software quality, and dramatically lower AI token consumption. The knowledge graph is simply
the durable memory that makes all of it possible.

---

## Appendix — Ratified design decisions

Three questions previously left open, now resolved for an enterprise, commercial platform:

1. **AI-only semantic concepts live in a separately-versioned semantic overlay, not the frozen core**
   (§11, §21). *Why:* the deterministic `@bkg/protocol` — which adapters, diffing, security, and the
   license boundary all depend on — must stay frozen and pure; soft, evolving semantic nodes version
   on their own cadence and never pollute the hard-facts contract or gate CI.
2. **Serve every AI fact, trust none silently; consumption is policy-gated by a confidence floor**
   (§8, §22). *Why:* on legacy enterprise repos a labeled lead beats nothing, but presenting opinion
   as fact is a liability. So `ai-*` facts are served with confidence + citations for exploration,
   `ai-inferred` is non-gating by construction, automated gates require `inferred`+, and
   security / breaking-change gates require `runtime-confirmed` — with an admin-tunable floor and a
   `bkg trust` maturity report.
3. **One team-canonical, CI-pinned AI baseline; model upgrades are explicit and reviewed** (§8, §13).
   *Why:* enterprises need reproducibility and audit. Teammates replay one shared, stamped lineage
   rather than each running a divergent fresh bootstrap; a model change is a diffed, reviewed baseline
   update, never silent, and determinism-critical contexts only ever run the pinned baseline.
