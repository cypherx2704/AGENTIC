# Incremental Code Knowledge Graphs — Prior Art & Strategy (bkg)

> Research deliverable answering: **"Is there any platform that builds a knowledge graph for a legacy or new codebase AND incrementally updates the affected nodes of the graph whenever the code changes?"** — plus the white-space verdict, threats, and the concrete techniques to steal for building the bkg incremental engine.
>
> Basis: a 15-agent research workflow (7 parallel finders across code-KG prior art, 7 adversarial claim verifications, high-effort synthesis; ~741k tokens). Confidence flags are preserved; the lowest-confidence marketing/paper claims are quarantined in the appendix. Planning only — no code.

---

## TL;DR

- **Yes, a handful of systems genuinely do incremental *graph* update — but none of them is a backend-semantic, agent-facing product.** The systems that truly recompute only affected nodes via reverse-dependency propagation are **Meta Glean**, **GitHub Stack Graphs**, **rust-analyzer (Salsa)**, the **rustc query system**, **Eclipse JDT / IntelliJ PSI**, and **Infer**. Every one is either general-purpose (no routes/DTOs/auth), language-locked, or an IDE/compiler internal — **none is served to AI agents over MCP, and none models a backend API.**
- **Most tools that *say* "incremental" don't do (III).** Kythe re-indexes whole compilation units; Sourcegraph SCIP is mostly full-reindex-per-commit in practice; Joern rebuilds the whole CPG; CodeQL only made *extraction* incremental (2026); and **every 2025–26 OSS "code graph for agents" clone** (codegraph, codebase-memory-mcp, code-review-graph, code-graph-mcp, Gortex) does watch → debounce → **re-parse changed files** with no reverse-dependency invalidation and no rebuild oracle.
- **White-space verdict:** bkg's exact 7-way combination — *local-first + framework-aware backend API graph + per-fact confidence/provenance + true incremental affected-node update + MCP + no-LLM-on-query* — **is unoccupied.** But it is narrower and more fragile than a "new category" story: the wrapper ("a local structural graph over MCP") is already commoditized in free OSS. **The defensible moat is exactly three things: (1) backend semantic DEPTH, (2) universal per-fact provenance/confidence, (3) *verified* incremental-graph correctness (a byte-identical rebuild oracle).**
- **Biggest strategic correction:** stop leading with "cut agent tokens." The token-savings margin is real but workload-dependent and shrinking (prompt caching + big context windows). Lead with **determinism, freshness, and correctness** — always-fresh, never-stale, provenance-tracked backend + data-flow traceability that live grep and embeddings cannot reliably produce. That also lands bkg on the *winning* side of the "agentic grep vs. persistent index" debate (see Part 6).

---

## Part 1 — The distinction that the whole question hinges on

Vendors routinely conflate three very different things. Getting this right is the difference between a moat and a weekend project.

| | What it is | What it says about the graph | Who does it |
|---|---|---|---|
| **(I) Incremental parsing** | Reuse unchanged CST subtrees on each edit | **Nothing** about the cross-reference graph | Ubiquitous — tree-sitter |
| **(II) Changed-file re-index / re-embed** | Detect changed files (Merkle/content hash), re-parse or re-embed only those | There is no graph whose edges get invalidated; the file's facts are recomputed wholesale | Most "incremental" AI tools: Cursor, Continue, and every OSS code-graph clone |
| **(III) True incremental graph/fact update** | Recompute only the *facts* (nodes/edges) whose **value** actually changed, propagating through **reverse dependencies**, with **early cutoff** so unchanged outputs don't cascade | The graph is surgically maintained; a comment edit touches nothing downstream | Rare: Glean, Stack Graphs, Salsa/rustc, JDT/PSI, Infer |

**bkg's claim ("incrementally update affected nodes") is a (III) claim.** If it ships as (I)+(II) — watch, debounce, re-parse the changed file — it has no moat, because every OSS competitor already does that. **(III) is the load-bearing, must-be-*tested* differentiator.**

> **Hard rule:** tree-sitter incrementalizes *parsing only*. "The syntax tree updated" is **not** "the cross-reference graph updated." These are two separate layers and must never be conflated in the design.

---

## Part 2 — Systems that genuinely do (III), and how (steal these mechanisms)

| System | Incremental mechanism | Granularity | Verdict |
|---|---|---|---|
| **Meta Glean** | Immutable **stacked DBs** + per-fact **ownership sets** (unit ≈ a file) propagated through the fact graph; derived facts carry ownership *expressions* (`{P}&{Q}`) enabling incremental re-derivation; target cost O(changes) | file / "unit" | CONFIRMED |
| **GitHub Stack Graphs** | Each file compiled to an **isolated partial graph** at index time; cross-file name resolution deferred to **query-time path-finding** — so file-level incrementality is true *by construction* | file | CONFIRMED |
| **rust-analyzer (Salsa)** | **Red-green demand-driven memoization** + **early cutoff** (backdating) + durability tiers | per-query | CONFIRMED |
| **rustc query system** | On-disk **DepGraph of fingerprinted DepNodes**; try-mark-green + output-fingerprint early cutoff, persisted across sessions | per-query | CONFIRMED (note: rustc does **not** use Salsa — it *inspired* Salsa) |
| **Eclipse JDT / IntelliJ PSI** | Persisted reference graph / per-file forward indexes; only **structural (signature) changes** propagate to dependents | type / file | CONFIRMED |
| **Infer (Meta)** | Compositional bi-abduction **procedure summaries**; `--reactive` re-analyzes only changed procedures | procedure | CONFIRMED |

Sources: [Glean incremental](https://glean.software/blog/incremental/) · [Glean OSS](https://engineering.fb.com/2024/12/19/developer-tools/glean-open-source-code-indexing/) · [Stack Graphs blog](https://github.blog/open-source/introducing-stack-graphs/) + [paper](https://arxiv.org/abs/2211.01224) · [Salsa algorithm](https://salsa-rs.github.io/salsa/reference/algorithm.html) · [rustc incremental](https://rustc-dev-guide.rust-lang.org/queries/incremental-compilation-in-detail.html) · [IntelliJ PSI stubs](https://plugins.jetbrains.com/docs/intellij/indexing-and-psi-stubs.html) · [Infer](https://engineering.fb.com/2017/09/06/android/finding-inter-procedural-bugs-at-scale-with-infer-static-analyzer/).

**The through-line:** every one keys facts by **stable nominal identity**, tracks **reverse dependencies**, and uses **early cutoff** (if a recomputed value equals the cached one, don't advance its revision → zero downstream cascade). These three ideas are the entire secret, and bkg must implement all three.

---

## Part 3 — Systems widely assumed incremental that are NOT (III)

- **Kythe** — whole-compilation-unit re-index + batch serving tables; maintainers concede "fundamentally no way to avoid the possibility of a complete re-index" (edit a core header → everything re-indexes). *Batch.* ([overview](https://kythe.io/docs/kythe-overview.html))
- **Sourcegraph SCIP** — *contested.* SCIP's string symbols were designed to *enable* per-changed-file indexing and Sourcegraph markets it, but in practice the auto-indexer re-runs a **full SCIP index per commit**; any real incrementality leaks in from the build system (Bazel), not reverse-dep graph invalidation. *Confidence: medium.* ([SCIP](https://sourcegraph.com/blog/announcing-scip), [auto-indexing](https://sourcegraph.com/blog/announcing-auto-indexing))
- **Joern (CPG)** — full CPG rebuild in production; incremental CPG exists only as a thesis. ([docs](https://docs.joern.io/code-property-graph/))
- **CodeQL** — query evaluation is **not** delta-incremental; a 2026 PR feature caches *extraction* and merges a small changed-code DB with a cached whole-repo DB (29–70% faster), then runs queries over the combined DB. True incremental Datalog eval remains research. ([changelog](https://github.blog/changelog/2026-03-24-faster-incremental-analysis-with-codeql-in-pull-requests/))
- **The 2025–26 OSS agent clones** (codegraph, codebase-memory-mcp, code-graph-mcp, GitNexus, grepai) — all do file-watch → debounce → **re-parse changed files**. codegraph's own docs state it does **not** recompute only affected nodes, has no reverse-dep invalidation, no content-addressed cache. This is (I)+(II).
- **Cursor / claude-context / Continue** — Merkle-tree change detection driving incremental **re-embedding** (II); no graph.

---

## Part 4 — Prior-art matrix by tier

**(a) Internal / infra code-KGs** — proven (III) machinery, but generic and not agent-facing.

| System | Truly incremental (III)? | Backend/API-aware? | Agent/MCP-served? |
|---|---|---|---|
| Kythe | No (batch) | No | No |
| **Glean** | **Yes** | No | No |
| **Stack Graphs** | **Yes** | No | No |
| GitHub `semantic` | archived 2025-04-01 | No | No |

**(b) Static-analysis / CPG** — deep on graph *semantics*, but security/bug-oriented and C/C++/Java-skewed. None models routes/DTOs/middleware/auth as first-class nodes. Local execution + persisted queryable graph are **table stakes** here (don't over-claim them).

| System | Incremental? | Backend-aware? | MCP-served? |
|---|---|---|---|
| Joern (CPG) | No (full rebuild) | Partial (taint, not structural) | Community MCP |
| CodeQL | Partial (extraction only) | Partial (hand-written queries) | Community MCP |
| Semgrep | Partial (diff-aware file selection) | No | Serves findings |
| SonarQube | Partial (result caching) | No | Serves issues |
| Doop / Soufflé | No (incremental is research) | No | No |
| **Infer** | **Yes** (summaries) | No | No |

**(c) AI-agent context** — embeddings or ephemeral maps; none is a persistent backend graph.

| System | Approach | Incremental? | Backend-aware? | MCP? |
|---|---|---|---|---|
| Claude Code | Agentic grep, **no index** | N/A | No | *Consumer* |
| Aider | Ephemeral repo-map (tree-sitter + PageRank) | Parse cache only | No | No |
| Cursor | Embeddings + vector DB | Re-embed (Merkle) | No | Internal |
| Windsurf | Embeddings + reranker + SWE-grep | Hybrid → agentic grep | No | Internal |
| Augment | Opaque semantic index | "real-time" (undisclosed) | No | Yes |
| Greptile | AST + LLM docstrings + embeddings | Re-embed (LLM on index path) | No | Yes |
| Sourcegraph Cody/Amp | Precise symbol xref graph | File-incremental at index layer (contested) | No | Yes |
| Serena | **Live LSP**, no persistent graph | Always fresh | No | Yes |

**(d) 2025–26 code-KG startups / MCP — the direct-competitor tier.** (Star counts and token-reduction headlines here are vendor self-reports; treat as **unverified** — see appendix.)

| System | Incremental (verified)? | Backend depth | MCP? | Notes |
|---|---|---|---|---|
| **codegraph** (colbymchenry) | **No** — watch+debounce+re-parse; docs say NOT affected-node | route→handler, ~17 frameworks | Yes | MIT; strongest *headline/adoption* match; viral |
| **codebase-memory-mcp** (DeusData) | Partial (RAM-first single dump; edge invalidation undisclosed) | Route + K8s Resource nodes, confidence-scored matching | Yes | Strongest *shape* match; **embeddings on query path** |
| **code-review-graph** (tirth8205) | **Closest to (III)**: diffs changed files, finds dependents via SHA-256, re-parses only what changed | **None** (generic call/import) | Yes | No routes/DTO/auth; no rebuild-oracle |
| code-graph-mcp (sdsrss) | Partial (BLAKE3 Merkle; under-links cross-file) | Route tracing (Express/Flask/FastAPI/Go) | Yes | Small |
| Gortex | Claimed (fsnotify ~200ms patch) | Normalized endpoint edges | Yes | In-memory (rebuilds on restart) |
| Potpie | Undocumented | Neo4j; endpoints as nodes; **AI docstrings in graph** | Yes | ~$2.2M pre-seed; LLM on query path; not local-first |
| Blar / Blarify | **No** (incremental = "future work") | No | No | LSP-based |
| cognee / GraphRAG / FalkorDB | No / cache-skip only; **LLM extraction** | No | Varies | Nondeterministic |
| Graphiti / Zep | **Yes** (bi-temporal) but **not code-structural** | No | Yes | Reference *pattern* for incremental KG; needs LLM to ingest code |

**(e) API / OpenAPI tools** — the only tier that natively understands routes/DTOs from code, but each is single-framework, one-shot, spec-only (no auth/middleware/env/queue graph, no persistence, no reverse-dep incrementality).

| System | Persistent code-derived graph? | Incremental graph? | Source | MCP? |
|---|---|---|---|---|
| @nestjs/swagger, FastAPI, tsoa, drf-spectacular, springdoc | No (one-shot spec, single framework) | No (whole-doc rebuild) | Code | No |
| oasdiff, Optic (archived Jan 2026 — *unverified*), Bump.sh | No (spec-in/diff-out) | Stateless whole-spec diff | Spec | No |
| Akita/Postman Insights, Treblle, APIToolkit | Runtime catalog | **Yes** (from traffic) | **Runtime traffic**, not code | No |
| Apollo GraphOS, Buf BSR | Registry (single protocol) | Change-scoped validation | SDL/proto | No |

---

## Part 5 — White-space verdict & the three closest competitors

**The full 7-way conjunction is unoccupied.** No one has *backend DEPTH (auth/DTO/middleware/env/queue/cron/data-flow) + universal provenance/confidence + verified incremental-graph + no-LLM-on-query + local + MCP* at once. **But** the adversarial verdict on "no product ships this exact combination" was **PARTIALLY_TRUE**: literally true for *commercial* products, yet the *stack* is already shipping in widely-adopted OSS. The single genuinely-rare attribute is **universal per-fact confidence + provenance**, which even the OSS leaders provide only partially.

**Closest competitors and precisely what they lack:**

1. **codebase-memory-mcp (DeusData)** — strongest *shape* match (local-first, SQLite, tree-sitter + LSP, first-class Route/Resource nodes, confidence-scored route↔call-site matching, 14 MCP tools, optional runtime trace ingestion). **Lacks:** deep framework semantics (no Nest/Fastify decorator / DTO-field / middleware-chain / auth-policy / role modeling; no env/queue/cron/event nodes); a universal static/runtime/ai/merged provenance taxonomy; **verified reverse-dep invalidation / rebuild oracle** (RAM-first "single dump" implies rebuild); and it puts **embeddings on the query path** (violates no-LLM-on-query).
2. **codegraph (colbymchenry)** — strongest *adoption* match (MIT, viral, "no embeddings, no LLM, 100% local"). **Lacks:** true incrementality (concedes it does not recompute only affected nodes); depth beyond route→handler; a real confidence/provenance model; ts-morph/TS-compiler semantic resolution (tree-sitter-only → shallow on typed TS, DI, decorators, generics); any open-core moat.
3. **code-review-graph (tirth8205)** — closest match on the **incremental mechanism** specifically (diff → find dependents via SHA-256 → re-parse only what changed). **Lacks:** all backend/API awareness; provenance/confidence; runtime; capped-AI proposer; a rebuild-oracle correctness claim.

**Bounding the space:** Sourcegraph SCIP (most mature deterministic incremental xref graph — but symbol-level, enterprise, not backend-semantic); Glean/Stack Graphs (proven incremental machinery — but generic, not agent-facing); Potpie (closest *commercial* code-KG-for-agents — but Neo4j, AI-docstrings-in-graph, LLM on query path).

**The defensible lane is backend depth + provenance + verified incrementality. "A code graph for agents" is not.**

---

## Part 6 — Threats & headwinds

**A. Agentic-grep vs. persistent index (the loudest headwind — but it targets *embeddings*, not structural graphs).** Anthropic built and **removed** RAG+vector DB from Claude Code (agentic search "outperformed everything, by a lot," and dodges "staleness, privacy, reliability" issues); Qodo removed its code-RAG index (ROI ≈ zero — *2026 date, medium confidence*); Windsurf shipped SWE-grep arguing embeddings are "counterproductive" for multi-hop traversal. **Reconciliation (verified):** this backlash is specifically against *approximate/stale vector RAG* — **not** against deterministic structural/symbol graphs, which the grep advocates *concede* beat "grep-and-read-whole-file." Cursor simultaneously doubled down on persistent embeddings. The industry is **split, not converged.** bkg is on the correct side of the line — **but only if it stops selling "token savings" as the headline** and sells freshness/determinism/correctness instead.

**B. Token-savings economics are eroding.** ~90%+ prompt-prefix caching + growing context windows shrink the arbitrage. Independent validation that structural indexing beats agentic grep at lower $/solved exists but is *workload-dependent* (payoff concentrates on multi-file changes; "small and noisy" per query otherwise) — and one supporting paper is **low-confidence/possibly-mis-attributed** (appendix). Net: token-savings is real-but-shrinking; do not make it the load-bearing pitch.

**C. Platform absorption (who could build this fastest).** **GitHub/Microsoft** already own Stack Graphs (proven incremental graph) + CodeQL (backend-ish semantics) + Copilot distribution. **Sourcegraph** owns the most mature incremental xref graph + MCP; route/DTO extractors are incremental for them. **Anthropic/Cursor/Windsurf** could fold a structural graph into the harness. Mitigation: move fast on the depth + provenance + verified-incrementality combination they'd have to build from scratch.

**D. Commoditization.** "Graph over MCP" is a weekend project; ≥6 OSS clones shipped in H1 2026. **codegraph's viral MIT distribution could define the category as "good-enough + free" before bkg ships depth — the single most acute competitive threat.** A paid product must lead with the hard moats, not the wrapper.

---

## Part 7 — What to steal for the bkg incremental engine (bridges to the build plan)

**Recommended architecture: a two-layer incremental engine.**
- **Layer A — a Salsa/DICE-style demand-driven memoized query graph** for extraction, persisted to SQLite. Every structural fact (one route, one DTO field, one middleware binding, one auth policy) is its **own** memoized query recording exactly which inputs it read.
- **Layer B — a delta-Datalog / incremental-view-maintenance layer** for derived cross-reference facts (call graph, data flow, auth reachability) where an input delta yields an output delta automatically.

**The nine techniques to adopt (each maps to real prior art):**

1. **Demand-driven memoized queries + global revision counter** (Salsa, Buck2 DICE, rustc). Target: O(invalidated subset) traversal, O(changed subset) recompute.
2. **Early cutoff / backdating** — *the single highest-leverage technique.* If a recomputed node's value equals the cached one, don't advance its changed-revision → downstream stays green (zero cascade). This is what makes a comment/whitespace edit cost nothing.
3. **Projection / firewall queries** (rustc) — store each fine-grained fact as its own node so editing one route in a file doesn't dirty consumers of the *other* routes in that file (else you silently degrade to file-granularity).
4. **Reverse-dependency invalidation with dirty-vs-changed** (Skyframe, DICE) — keep explicit rdeps; on edit, cheaply mark the reverse-transitive closure *dirty*, recompute **only on demand** when an MCP query reads a node. Copy Skyframe's *dirty* ("recheck") vs *changed* ("value differs") distinction.
5. **Content-addressed slice hashing + the rebuild oracle** (Bazel Remote Execution Merkle design, BLAKE3) — a node's cache key = `digest(slice content + digests of every fact it read)`. This gives the cache key **and** the byte-identical rebuild oracle simultaneously: periodically do a clean full rebuild and assert incremental digests match — **any divergence is an invalidation bug.**
6. **Stable, path-based nominal identity** (rustc `DefPathHash`) — key nodes by `module-path + symbol` / `route method+path` / `DTO qualified name`, **never** by array index or byte offset (the #1 way naive systems collapse back to full recompute).
7. **Cross-file resolution at query time** (Stack Graphs) — the crux for route mounting. Extract each file into an isolated partial graph (incremental, file-local); resolve cross-file references (`app.use('/api', router)`, Nest `@Module` imports, FastAPI `include_router`) at **query time** by stitching partial graphs. Do **not** store eager cross-file edges (they'd depend on arbitrary other files and break incrementality). Materialize cross-file edges (call graph, "endpoints touching table X") only in Layer B as incremental Datalog.
8. **Blast-radius recompute-vs-rebuild threshold** (Soufflé "elastic") — incremental doesn't always win; for a branch switch / dependency bump / mass rename, a full re-index is cheaper. Estimate blast radius per change and switch strategy.
9. **Cycle handling** (backend graphs have cycles) — SCC-condensation for pure structural facts; fixed-point iteration over a fixed-height lattice for monotone derived facts ("is this endpoint transitively auth-protected", taint); stratified negation for soundness.

**Persistence gap you must fill yourself:** Salsa/DICE are in-memory; rustc persists but isn't a usable library; Bazel RE caches *opaque* outputs, not a queryable graph. **Persist the memo table in SQLite as `key → {value, deps, fingerprint, revision, provenance, confidence}`.** A durable, queryable, incrementally-maintained code-structure graph served over MCP with no LLM on the query path is genuinely greenfield.

---

## Part 8 — Corrections to bkg's technical assumptions (from the research)

- **R1 — Reposition off "token savings."** It's real but workload-dependent and shrinking (caching + context windows). **Lead with determinism, freshness, and correctness** — always-fresh, provenance-tracked backend + data-flow traceability (route→middleware→auth→DTO→DB, cross-service flow, blast radius) that live grep and embeddings can't reliably produce. Answer Cherny's "staleness/reliability" objection, not the token-arbitrage one.
- **R2 — If "incremental" = watch→debounce→re-parse, there is no moat.** The differentiator is (III): reverse-dep invalidation + content-addressed caching + a *proven* byte-identical rebuild oracle. Ship the oracle as a CI gate **and** a user-facing correctness guarantee, or the differentiation is vapor.
- **R3 — "70/30 generic/adapter" is optimistic; the depth *is* the adapter work.** Every competitor stops at route→handler precisely because deeper (Nest DI/decorators/generics, DTO fields, middleware chains, auth policies) is framework-specific and needs **ts-morph / the TS compiler API**, not tree-sitter alone. Budget the framework-extraction + semantic-resolution layer at **~50%+** of engineering effort (medium confidence). That's not a bug — **it's where the moat is.** Depth-per-framework is the wedge; breadth-of-languages is not.
- **R4 — Cross-file route mounting must be confronted head-on.** `app.use('/api', router)`, Nest `@Module`/`imports`, FastAPI `include_router(prefix=...)`, mount-time middleware — the final route path + guard chain + DTO binding are assembled from **multiple files**. A naive "re-parse changed file" produces **stale route facts** when a mount point or parent middleware changes elsewhere. **Mandate:** never store eager cross-file route edges; extract per-file partial facts in isolation and compute the fully-qualified route at query time (Stack Graphs), or materialize it in Layer-B incremental Datalog with proper reverse-dep tracking. Competitors that "prefer same-file targets" are quietly *under-linking* to dodge this — bkg cannot, because route mounting is exactly the cross-file case backend developers care about.
- **R5 — Provenance/confidence is bkg's rarest asset; keep the AI proposer verifiably off the source-of-truth path.** The static/runtime/ai/merged taxonomy + capped proposer is the correct *post-RAG* architecture and a genuine trust differentiator (no competitor has universal per-fact provenance). **Risk:** the moment an AI-proposed fact leaks into a "static"-provenance node or onto the query path, bkg inherits the very nondeterminism the deterministic camp is winning against. Enforce the cap architecturally.
- **R6 — Runtime observation is unoccupied whitespace but unproven surface.** Merging runtime traffic with static truth is absent from every competitor. Real differentiator — but treat it as **corroboration** (raise confidence, discover dynamic/undeclared routes), never authoritative over static facts, and keep it strictly optional so the local-first, no-daemon story holds.
- **R7 — Don't over-claim table stakes.** Local execution, persisted-queryable graph, and "no LLM on the query path" already exist in Joern, CodeQL, SCIP, Serena, and the OSS trio. Differentiation must lean entirely on **backend depth + universal provenance/confidence + verified incremental-graph correctness + structural API diff (blast radius across commits)** — the genuinely open combination.

---

## Appendix — Confidence caveats (read before quoting in a deck)

The research surfaced several claims that are **unverified or low-confidence**; do not cite them as fact:
- **Star counts** for OSS competitors (codegraph "47–59k", codebase-memory-mcp "29k") — implausible / vendor- or aggregator-sourced; **treat as unverified**.
- **Token-reduction headlines** (codebase-memory-mcp "99% / 120x", various "49–528x") — cherry-picked vendor self-reports.
- **Augment's "AST + dataflow + CFG + GNN" architecture** — third-party lore, not confirmed by Augment.
- **Specific 2026-dated events** (Qodo v2.4 code-RAG removal "July 2026", Optic "archived Jan 2026") — plausible and directionally consistent with the trend, but past the assistant's knowledge cutoff; verify before relying.
- **The independent token-savings validation paper** ("Code Isn't Memory", arXiv 2606.22417, and IDs 2603.27277 / 2601.08773) — the papers may be real per abstract, but the specific mechanism attributions (Merkle/reverse-dep/route-aware) were flagged by the synthesizer as summary padding, **not** in the verified abstract. Treat the "structural indexing beats grep at lower $/solved" finding as **directional, medium confidence.**

The **high-confidence, load-bearing findings** — the (I)/(II)/(III) taxonomy; which systems genuinely do (III) and their mechanisms (Glean, Stack Graphs, Salsa/rustc, JDT/PSI, Infer); Kythe/SCIP/Joern/CodeQL/OSS-clones being (I)/(II); the white-space verdict; and the nine build techniques in Part 7 — are well-sourced and safe to build on.
