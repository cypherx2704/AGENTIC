# bkg — Knowledge-Graph Core: Build Plan

> The executable plan for building **the heart of the product**: the deterministic, incrementally-updated backend knowledge graph. Companion to the design doc [updated-plan.md](updated-plan.md) and the prior-art research [bkg-research-incremental-code-kg.md](bkg-research-incremental-code-kg.md). Scoped to the CORE only — the AI proposer, runtime observation, and testing engine are later features. Planning only — no code yet.
>
> Basis: a 3-architect panel (correctness-first, value-slice-first, engine-first) + high-effort synthesis. Anchored on determinism-oracle-first, grafting the engine rigor and the product-slice discipline of the other two.

---

## AMENDMENTS (2026-07-11) — Python engine · FastAPI-first · DB behind a port

Three decisions supersede the TypeScript/Express specifics in the body below. Where the body says TS/Express/ts-morph, read the mappings here. Everything about the *architecture* (determinism-oracle-first, the memoized incremental engine, early cutoff, projection/firewall queries, stable identity, content-addressed hashing, query-time cross-file stitching, the four oracle invariants) is unchanged and language-agnostic.

**A1 — The engine is written in Python** (not TypeScript). Rationale: the first (and near-term only) target is the founder's Python FastAPI projects, and deep FastAPI/Pydantic analysis runs *in-process* in Python (LibCST / Jedi / mypy + Pydantic's own `model_json_schema()`), which is exactly the depth that is the moat. Stack mapping:

| Design doc (TS) | bkg (Python) |
| --- | --- |
| pnpm monorepo `@bkg/*` | one `bkg` package, src-layout, clear module boundaries (split to a uv workspace when the OSS/license boundary matters) |
| zod frozen protocol | **pydantic v2** frozen models (`src/bkg/protocol/`) |
| BLAKE3 (js) | **`blake3`** (PyPI) — `src/bkg/protocol/canonical.py` |
| better-sqlite3 | **stdlib `sqlite3`** (WAL), behind the port |
| fast-check (property tests) | **hypothesis** + deterministic `random.Random(seed)` |
| web-tree-sitter (structure) | **py-tree-sitter** (P3) |
| ts-morph (depth) | **LibCST / Jedi / Pydantic introspection** (P4) |
| `@modelcontextprotocol/sdk` | **Python MCP SDK** (P3+) |

**A2 — First adapter is FastAPI, not Express.** The `RouterMount` IR was already frozen to cover `include_router` (§6), so the protocol is unchanged. Roadmap deltas: **P3** builds the real **FastAPI** adapter (`@app.get`/`APIRouter`/`include_router(prefix=)`/path+query params/`Depends` → `GUARDED_BY`); **P4 depth** resolves **Pydantic models** (fields, types, `response_model=`, nested models) instead of ts-morph types; **P5's** second-framework stub becomes another Python framework (Flask/Django REST) or a TS framework via a Node sidecar. Express/Nest and other TS frameworks move to post-core breadth (via the same `Adapter` contract + a Node sidecar for TS depth — the mirror of the doc's original Python-sidecar plan).

**A3 — The database is hidden behind the `GraphStore` port** (strengthened from §8's swap seam). The engine, pipeline, and adapters depend only on the `GraphStore` ABC + the `open_store()` factory; exactly **one** module (`src/bkg/store/sqlite_store.py`) imports `sqlite3`. Swapping to another store later = one new subclass, zero changes elsewhere.

**Location:** the project lives at `AGENTIC/backend-knowledge-graph/` (this folder), fully isolated from `CoreProjects/cypherx-a1/`.

### ✅ P0 status — DONE and verified (2026-07-11)

P0 is implemented and green: frozen protocol (`bkg.protocol`), canonical serializer + BLAKE3 (`bkg.protocol.canonical`), the `GraphStore` port + `SqliteGraphStore` (`bkg.store`), the materialize/snapshot bridge (`bkg.snapshot`), and the **determinism harness** (`tests/`). Verified: **8/8 pytest green** (byte-identical snapshot across 100 repeats, 25 shuffled insertion orders, and teardown/reload; OS-independent by construction; single-fact change isolates to a single fingerprint) + **ruff clean** + **mypy clean**. Next: **P1** — the demand-driven memoized incremental engine + the 4-invariant oracle on a synthetic graph, before any FastAPI parsing.

---

## 0. Context — the one idea that shapes everything

The whole product depends on the knowledge graph, so we build the graph first. But the research is unambiguous about *what makes it defensible*: **"a code graph over MCP" is already commoditized free OSS (≥6 clones).** The moat is exactly three things, and only these:

1. **Backend semantic DEPTH** — routes + DTO fields + middleware chains + auth + env/queue/cron + data flow, not just route→handler (every competitor stops at route→handler because deeper is framework-specific and expensive; that expense *is* the moat).
2. **Universal per-fact provenance + confidence** — no competitor has this on every fact.
3. **VERIFIED true-incremental correctness** — recompute *only* the facts whose value changed (type (III)), proven by a **byte-identical rebuild oracle**. Everyone else does watch → re-parse changed file (type (II)); that is not a moat.

**Two corrections from the research bake into this plan:**
- **Reframe off "save tokens"** (shrinking margin, and the agentic-grep backlash targets *embeddings*, not deterministic structural graphs). Lead with **freshness, determinism, correctness** — always-fresh, never-stale, provenance-tracked backend + data-flow traceability that live grep and embeddings can't reliably produce. Token-savings becomes a *secondary* demo, not the headline.
- **Cross-file route mounting is the crux** (`app.use`, Nest `@Module`, FastAPI `include_router`). It must be solved by per-file partial facts stitched at **query time**, never eager cross-file edges — or "re-parse the changed file" silently ships stale routes.

**The single most important sequencing decision (amends the design doc):** the design doc places the sync engine in Phase 2, *after* Express and breadth. **We pull the memoized incremental engine + the determinism oracle to be the second thing built — before the real parser, before any framework breadth.** Every pipeline stage is authored as a fine-grained memoized query *from birth*, so early cutoff can fire per-fact. Retrofitting incrementality onto an already-monolithic assembler is exactly how (III) degrades to (II). We prove the moat on a synthetic graph before a single real file is parsed.

---

## 1. Strategy (why correctness-first)

Three approaches were designed and scored:

- **Determinism-oracle-first** — proves (III) before any parser exists; strongest moat alignment and minimal scope. **Anchors this plan.**
- **Walking-skeleton (value-slice-first)** — best cure for the "engine that serves nothing" trap, but its load order ships a full-rebuild serving path first and slides the engine underneath later — the retrofit path that degrades (III)→(II). **Grafted:** its slice discipline (reach a real `getEndpoint` mid-roadmap, not at the end), Layer-B deferral, typed-only DTOs, freshness-over-tokens framing. **Rejected:** the full-rebuild-first serving path.
- **Engine-first + Layer B** — best architecture (clean Layer-A/Layer-B seam, projection/firewall discipline, adapter conformance harness), but pulls the delta-Datalog Layer B *into* the core — too much surface for a 1–3 person team. **Grafted:** the two-layer seam (with B deferred), the conformance harness, projection nodes. **Rejected:** Layer-B-in-core.

**Net:** de-risk verified (III) + the oracle earliest (correctness-first), reach a demonstrable real `getEndpoint` slice mid-roadmap (product discipline), keep Layer B out of the core (minimalism).

---

## 2. Scope — what "the core" is, and what it is not

**In the core (this plan):**
`@bkg/protocol` (frozen model) → canonical serializer + BLAKE3 digest → SQLite `GraphStore` + memo store → **Layer-A demand-driven incremental engine + determinism oracle** → the extraction/resolution/assembly/merge pipeline authored as fine-grained queries → `adapter-express` (real parsing) → ts-morph schema **depth** → thin `@bkg/mcp getEndpoint` + minimal `@bkg/cli` + minimal `@bkg/daemon`.

**Explicitly deferred — built only after the engine + oracle are green on Express with real depth** (each leaves a *declared-but-inert* seam so it slots in without reworking the engine):
- **AI proposer** (`@bkg/ai`, subagents, pin/replay, sealed-replay). Seam: `ConfidenceMerger` designed as a one-directional third input; `ai`/`ai-proposed`/`ai-inferred` + `verificationStatus` reserved in protocol; AI-replay/merged oracle gates stubbed as no-ops (trivially hold with zero AI facts).
- **Runtime L3/L4** (`@bkg/runtime`). Seam: `Provenance=runtime` reserved; merger tier order already accommodates promotion/conflict.
- **Testing engine** (`@bkg/testing`) and sandbox.
- **Layer-B delta-Datalog / IVM** (call graph, data flow, auth reachability). Blast-radius v1 rides the Layer-A reverse-dep closure. Seam: EDB projection + `LayerB.applyDelta(added, removed)`, throttled by Layer-A early-cutoff hits (a cutoff hit ⇒ no delta); SCC/fixed-point machinery lands *with* B.
- **Breadth** beyond Express + one P5 stub (full Nest/Fastify/FastAPI, sidecar adapters). Seam: the frozen `Adapter` contract + `RouterMount` IR already cover all three mounting shapes.
- **Untyped-Express schema inference** (the "schema is theater" risk) — P4 does typed/validated DTOs only.
- Cloud snapshot / team sync / OIDC; OpenAPI/docs/viz/diff/security/review; the semantic overlay; a second `GraphStore` backend (SQLite only — the interface is the seam, not a mandate to build two).

---

## 3. The core graph model to freeze in `@bkg/protocol`

Freeze exactly what the hero **Endpoint** payload requires; mark everything else reserved-unstable so it can evolve without breaking the frozen contract.

- **Nodes (frozen):** `File`, `Symbol`, `Route`, `Handler`, `Middleware`, `SchemaRef`, `Field`, `Endpoint` (derived hero).
- **Edges (frozen):** `HANDLES` (route→handler), `GUARDED_BY` (route→middleware/guard, ordered), `VALIDATES_WITH` (handler→body schema), `RETURNS` (handler→response schema), `MOUNTS` (app/router→router).
- **IR (frozen):** `RouterMount {mountingFile, routerLocal, prefix, middleware[], targetSymbolRef}` · `SymbolRef {name, fromFile, resolved?}` · `Field {name, type, required, format, ref, source, confidence}`.
- **Enums (frozen):** `Confidence` (`ai-inferred` ≤ `ai-proposed` < `inferred` < {`runtime-confirmed`, `static-certain`}) · `Provenance` (`static|runtime|ai|merged`) · `VerificationStatus` — the `ai*` and `runtime*` values are **declared but unused** in the core.
- **Endpoint hero payload:** `{method, resolvedPath, params, body:SchemaRef, response:SchemaRef, auth, middlewareChain[] (ordered), handler:{file,line}, confidence, provenance, verificationStatus}`.

**Deferred / reserved-unstable:** `AuthPolicy`/`Role`/`Permission` depth, `Service`, `DbModel`, `EnvVar`, `Event`, `Queue`, `CronJob`, call-graph/data-flow edges, the semantic overlay. (For now, auth in the hero payload is carried by the ordered `Middleware` + `GUARDED_BY` chain, not a rich policy model.)

Freezing `RouterMount` now is deliberate: it must cover Express `app.use`, Nest `@Module` imports, *and* FastAPI `include_router` (§6), so the second framework later needs no protocol change.

---

## 4. The incremental engine (Layer A) — the moat, lands EARLY (P1)

Authored as the substrate the pipeline sits on — **not** a naive full-rebuild baseline retrofitted later. A `freshRebuild(state)` function exists from P1, but **only** as the oracle's reference; the product path is incremental from birth.

**Memo table (SQLite / better-sqlite3 / WAL — persisted so incrementality survives a restart):**
```
memo(key TEXT PK, kind TEXT,
     value BLOB,            -- canonical-serialized fact
     value_fp BLOB,         -- BLAKE3(kind ⊕ canonical value); the early-cutoff fingerprint
     changed_rev INT,       -- revision the VALUE last actually CHANGED (backdating anchor)
     verified_rev INT,      -- revision last confirmed green
     provenance TEXT, confidence TEXT, partial INT)

deps(node_id TEXT, dep_id TEXT, dep_fp_at_read BLOB, PK(node_id, dep_id))
     INDEX rdeps ON deps(dep_id)   -- materialized reverse-dep = cheap invalidation AND blast-radius

inputs(input_id TEXT PK, content_fp BLOB, changed_rev INT)
meta(global_revision INT)
```

- **Reverse-dep tracking:** the `rdeps` index gives O(closure) dirty-marking (not O(all nodes)) and *doubles* as the blast-radius reverse index — no separate structure.
- **Early cutoff / backdating (highest leverage):** on edit, `setInput(id, contentFp)` bumps `global_revision = R` and sets the input's `changed_rev = R`. On demand, `query(key)` runs **try-mark-green**: recursively bring each dep up to date at R; if every `dep.changed_rev ≤ key.verified_rev` → green, set `verified_rev = R`, **no recompute**. Otherwise recompute and compute the new `value_fp`: if it equals the stored fingerprint → **backdate** (hold `changed_rev`, set `verified_rev = R`) so the cascade stops dead; else set `changed_rev = R` and propagate. *This is what makes a comment/whitespace edit cost nothing downstream.*
- **Dirty-vs-changed (Skyframe):** a file write cheaply marks the reverse-transitive closure *dirty* (needs recheck) with no recompute; *changed* is decided lazily by fingerprint only when a query actually pulls the node.
- **Projection / firewall queries:** high-fanout coarse nodes (`fileText:{path}`, `fileAst:{path}`) are immediately narrowed by cheap projections (`routeDeclList:{file}`, `exportMap:{file}`, `importMap:{file}`); **every per-fact node depends on a projection, never on raw AST.** Editing route A's body recomputes `fileAst` + `routeFacts(A)`, but `routeFacts(B)` re-fingerprints identical and early-cuts; adding a blank line early-cuts at `routeDeclList` (same id-set), so no route node is dirtied at all.
- **Stable nominal identity keys (rustc `DefPathHash` discipline — never byte offset / array index):**
  - inputs: `file:{repoRelPath}`
  - projections: `routeDeclList:{file}`, `exportMap:{file}`, `importMap:{file}`
  - per-file facts: `routeDecl:{file}:{routerLocal}:{method}:{literalPath}`, `handler:{file}#{symbol}`, `schemaDecl:{file}#{QualifiedName}`, `mwBind:{file}:{routerLocal}:{targetSymbol}:{declOrderOrdinal}`
  - resolution: `resolveRef:{refKey}`, `mountChain:{routerSymbol}`
  - derived: `endpoint:{method}:{resolvedPath}`
- **Content-addressed slice hashing:** `value_fp = BLAKE3(kind ⊕ canonical value)` is *simultaneously* the cache key and the oracle value — a passing cache is a passing oracle by construction.
- **Cycles:** Layer A is acyclic by construction (`fileAst → projection → facts → resolveRef → mountChain → endpoint`). SCC-condensation + fixed-point over a fixed-height lattice are reserved for the deferred Layer B.

**Why P1, before the real parser:** this is the correctness-first choice. If the engine is authored on synthetic queries and proven with the oracle first, the real adapter (P3) drops onto a substrate that is already provably incremental. Building the assembler first and the engine second is the trap.

---

## 5. The determinism oracle — the proof, a blocking CI gate

Property-based (fast-check). Seed `S0`; build `freshRebuild(S0)`; apply a randomized edit sequence `e1..en` (add route, change `app.use` prefix, rename handler, add/remove middleware, comment/whitespace edit, delete/recreate file, reorder routes). After **each** edit, maintain `G_incremental` via the engine and assert **four** invariants:

1. **Byte-identical:** `canonicalDigest(G_incremental) === canonicalDigest(freshRebuild(S_i))`.
2. **Fingerprint-map equality:** `{key → value_fp}` equals a fresh build's — pinpoints the divergent node and catches *latent stale deps* a snapshot alone would pass.
3. **Zero-cascade counter invariant:** a comment/whitespace edit advances **0** downstream `changed_rev`; a single-fact edit recomputes only its dependents. *This is the one that catches silent (III)→(II) regression — the byte-oracle alone would pass a system that secretly rebuilds everything.*
4. **rdep-completeness / no-stale-green:** no node read by the snapshot has `verified_rev < R`.

Half 1 (serialization determinism across shuffle/reload/OS) is already gating from **P0**. Invariants 1–4 go blocking from the **first engine commit** (P1). Fixed PRNG seed in CI + a random-seed nightly run. **Divergence rate > 0 = P0 stop-ship** — bisect the invalidation bug before any feature work. The oracle proves the graph is *right*; invariants 3–4 prove it stays *incremental*. Both are load-bearing, or the moat is vapor.

---

## 6. Cross-file route mounting — query-time stitching, never eager edges

Universal model: `parseFile` emits only **file-local facts + unresolved `SymbolRef`s + `RouterMount`s**. The fully-qualified path + guard chain + DTO binding are assembled at **query time** by memoized stitching queries whose `cx.read()` calls record cross-file memo deps. Critically, `resolveRef` depends only on the target file's **`exportMap` projection**, not its whole AST — so an unrelated edit in the target file early-cuts. Editing a mount prefix recomputes only `mountChain` + the dependent `endpoint` nodes; sibling routes early-cut → the "naive re-parse ⇒ stale route" failure is structurally impossible.

- **Express:** `app.use("/api", router)` → `RouterMount{mountingFile, routerLocal:"app", prefix:"/api", middleware:[…], targetSymbolRef:"router"}`. `mountChain(router)` walks the mount stack to accumulate prefix + ordered middleware; `endpoint = localRoute ⊕ mountChain`. **Implemented in core (P3).**
- **Nest:** `@Module({imports, controllers})` → module-membership + `@Controller('prefix')` + method-decorator facts; `@UseGuards` → `GUARDED_BY`. `mountChain` assembles the module-import tree + controller prefix; DI/decorator/generics resolution via **ts-morph**. **Second-framework shape; `RouterMount` IR frozen now to cover it.**
- **FastAPI:** `include_router(r, prefix=)` → `RouterMount` analog; `APIRouter(prefix=, dependencies=[…])` contributes prefix + guard chain; `Depends(...)` → `GUARDED_BY`. Same `mountChain` stitch.

All three collapse to identical `RouterMount` + `mountChain` machinery — proven first on a **synthetic 2-file mount (P1)**, then the real Express fixture (P3: edit prefix → re-query < 150 ms, byte-identical rebuild).

---

## 7. Phased roadmap — CORE only

| Phase | Goal | Key deliverables | Exit criteria | Size |
|---|---|---|---|---|
| **P0** Contract + serializer + memo store + harness | Bit-stable foundation the oracle stands on | Frozen `@bkg/protocol`; `canonicalize() → Uint8Array` + BLAKE3 `digest`; `GraphStore` + memo/deps/inputs/meta schema (better-sqlite3/WAL); golden + shuffle/OS-matrix harness | Hand-authored `PartialGraph` → byte-identical snapshot across 100 runs, shuffled insertion order, teardown/reload, and a 2nd OS. CI green. | **S** |
| **P1** Layer-A engine + oracle (synthetic) | THE MOAT PROOF — no parser yet | Demand-driven engine (revision + rdeps + try-mark-green + backdating early cutoff + Skyframe dirty/changed); projection/firewall pattern; synthetic 2-file mount; **oracle + zero-cascade counter + fingerprint-map + no-stale-green**, blocking CI | Oracle byte-identical over random edit sequences; no-op edit → 0 cascade; single edit → only dependents recompute; synthetic prefix edit updates the endpoint while sibling routes stay green. Divergence rate = 0. | **M** |
| **P2** Real pipeline as queries + stitching + merger | Author the actual pipeline on hand-authored multi-file input | `exportMap`/`importMap`/`resolveRef`/`mountChain`/`assembleEndpoint` as firewall-granular queries; `ConfidenceMerger` (static-certain vs inferred; the AI-third-input seam inert); hero Endpoint golden | Hand-authored 2-file mount → correct hero Endpoint; edit route A leaves route B's Endpoint green; an unrelated edit in a resolved target file early-cuts; oracle green | **M** |
| **P3** Real Express adapter + thin MCP (demonstrable slice) | Real parsing feeds the proven engine; serve a real query | `@bkg/adapter-sdk` + `adapter-express` (tree-sitter: routes/handlers/middleware → `PartialGraph`); content-addressed file inputs; **adapter conformance harness** (purity + stable-id); thin in-process stdio `@bkg/mcp getEndpoint`; minimal `@bkg/cli` | Real Express fixture → correct Endpoints; comment edit → 0 cascade; edit `app.use` prefix → only affected recompute, re-query < 150 ms; oracle green on real edits; MCP answers `getEndpoint` with the agent reading 0 files; conformance rejects an impure / unstable-id adapter | **M/L** |
| **P4** Tier-2 schema DEPTH (ts-morph) + blast-radius | The wedge — backend semantic depth | **ts-morph** type resolution: DTO fields / generics / decorators → `SchemaRef` + Field IR (`source: validation-lib \| static-type \| destructuring`); cross-file DTO binding as a query; `blastRadius` over the rdep closure (no Layer B) | Cross-file typed body/response schema on the Endpoint; edit a DTO field → only binding Endpoints recompute; `blastRadius(Dto)` correct and *stays* correct incrementally; oracle green | **L** |
| **P5** Core-complete demo + second-framework seam | Prove the freshness headline + swappability | Freshness demo (edit prefix → re-query < 150 ms, never stale); `scripts/demo-tokensave.ts` (Arm A: fs+grep vs Arm B: MCP, SDK-metered); minimal `@bkg/daemon` single-writer watch→requery; **second adapter STUB** (Fastify or Nest: detect + basic routes) | Agent answers "test the login endpoint" in ≤ 500 tokens vs a measured 30k+ baseline (≥ 90% reduction), correct joined path + cross-file schema, 0 files read; edit → requery < 150 ms, never stale; the second adapter reuses the engine **unchanged** | **M** |

**Real ts-morph depth enters at P4** (the ~50% wedge — tree-sitter alone can't resolve Nest DI / generics / DTO types; see research R3). **The second framework enters at P5** only as a swappability stub — full Nest/Fastify/FastAPI breadth is post-core.

---

## 8. Interfaces to FREEZE early

```ts
// Adapter — deterministic, pure, file-local; emits PartialGraph only
interface Adapter {
  id: string; capabilities(): Capabilities;
  detect(project): Promise<DetectResult>;
  parseFile(uri, src, ctx): Promise<PartialGraph>;   // local facts + symbolRefs + routerMounts; NOTHING cross-file
  resolveLocal?(graphs, ctx): PartialGraph;
}
interface PartialGraph { nodes; edges; symbolRefs: SymbolRef[]; routerMounts: RouterMount[]; }

// GraphStore — the swap seam (SQLite only for now)
interface GraphStore {
  getNode(id): MemoRow | undefined; putNode(row);
  getDeps(id): Dep[]; putDeps(id, deps); getRdeps(depId): NodeId[];
  getRevision(): number; bumpRevision(): number;
  txn(fn); snapshot(): Buffer;
}

// Memo / query API — Salsa/DICE red-green
type Query<K, V> = (key: K, cx: Cx) => V;
interface Cx { read<T>(dep: NodeId): T; }             // read() records a dependency edge
Engine.defineQuery(kind, fn); Engine.query(id): Value; Engine.setInput(id, contentFp);
canonicalize(x): Uint8Array; digest(bytes): Hash;
```

Assembly, symbol resolution, confidence, merge, storage, serialization, and stitching are the **core's** job — never a plugin's. The `AiAnalysisProvider` (proposal-only) and the Layer-B `applyDelta(added, removed)` interfaces are *declared* as inert seams at P2/P4.

---

## 9. Risks & mitigations + verification

**Risks → mitigations**
1. **Monolithic assembler kills early cutoff** ((III)→(II) collapse). → Firewall/projection queries per fact *from birth* (P2); the zero-cascade recompute-counter is a first-class CI gate; a fanout audit flags any node reading raw `fileAst` directly.
2. **Non-canonical serialization poisons the oracle** (map order, wall-clock, absolute paths, float format). → A single frozen serializer at P0; shuffle/OS matrix; zero clocks / abs-paths on the deterministic path.
3. **Byte-offset / array-index identity** → spurious cascade or full-recompute collapse. → Nominal path-based ids only, enforced by the conformance harness + lint.
4. **Latent stale deps** (snapshot correct now, diverges later). → The memo-table `value_fp` + dep-set equivalence gate (oracle invariant 2).
5. **Over-depending `resolveRef`** (reads the whole target file) → stale routes. → Depend only on the `exportMap` projection; an explicit "unrelated edit early-cuts" assertion (P2).
6. **Eager cross-file mount edges** → break file-local incrementality. → Architecturally banned; query-time stitching only.
7. **ts-morph type-checker cost on the hot path** → blows the < 150 ms budget. → Memoize symbol/type resolution as its own query node; demand-driven, so it runs only for queried endpoints.
8. **"Engine that serves nothing"** (the value-slice trap). → Pull the thin MCP `getEndpoint` slice to P3, right after the first real adapter — a demonstrable, valuable slice mid-roadmap, not at the end.
9. **Scope creep** pulls AI/runtime/testing into the core. → Hard package/dependency boundaries (`eslint-plugin-boundaries`); `ai` slots declared-but-unused; the core has zero AI/runtime code.

**End-to-end verification story:** (1) per-stage golden snapshots (P0 round-trip, P2 assembled Endpoint, P3 real Express Endpoints); (2) the byte-identical determinism oracle as a blocking gate from the P1 engine commit, divergence rate 0; (3) memo-table fingerprint + dep-set equivalence for latent staleness; (4) zero-cascade + no-stale-green counter invariants (proves it stays *incremental*, not merely *correct*); (5) cross-file mount surgical-recompute tests; (6) the shuffle/OS serialization matrix; (7) latency (< 150 ms re-query) + the token-savings harness (≥ 90%). *The oracle proves the graph is right; the counter invariants prove it stays incremental — both are load-bearing.*

---

## 10. Reconciliation with `updated-plan.md`

- **Endorsed as-is:** Phase 0 (skeleton + frozen protocol + SQLite + `Adapter` interface + fixtures + golden harness); the "build first = `@bkg/protocol` + `@bkg/core` GraphStore + the resolution pipeline on a hand-authored `PartialGraph` *before any parser*"; the two thin plugin contracts; the `Endpoint` hero payload; the confidence/provenance model; the AI-schema-slots-declared-but-unused discipline; the `GraphStore` swap seam with SQLite-only for now.
- **Amended — engine placement:** the doc puts the sync engine in Phase 2 after Express + breadth. **Pull the memoized engine + oracle to be the second milestone (our P1), before the real parser and before breadth.** Rationale: every stage must be authored as a fine-grained query from birth or early cutoff can't fire per-fact; retrofitting is the (III)→(II) trap.
- **Amended — the Phase-1 exit:** the doc leads its exit with the token-savings number. **Split it:** *freshness/determinism* (edit → re-query < 150 ms, oracle green, never stale) is the **primary** proof; token-savings (≥ 90%) is the **secondary** demo (research R1 — don't hang the product on a shrinking margin).
- **Amended — oracle timing:** blocking from the **first engine commit** (P1), not "the first sync sprint" — that is precisely when incremental sync first exists.
- **Consistent with the doc's own risk ledger:** untyped-Express schema inference stays deferred (P4 does typed/validated DTOs only — the doc's "schema is theater" risk #1); no sidecars/AI/runtime in the core (risk #4/#17); the determinism oracle is P0 (the doc's stance).

---

## Appendix — the first sprint (P0), concretely

To make P0 immediately actionable, the first sprint is four things in order, each small and independently testable:

1. **`@bkg/protocol`** — the frozen node/edge/IR/enum vocabulary of §3 as zod schemas + TS types, zero deps. Include the `ai`/`verificationStatus` slots, declared but unused.
2. **`canonicalize(graph) → Uint8Array` + `digest = BLAKE3`** — deterministic traversal order (sort by nominal key), no wall-clock, repo-relative paths only, fixed float formatting. *This is the true bedrock — the entire oracle reduces to `canonicalDigest(incremental) === canonicalDigest(rebuild)`, so if graph→bytes isn't bit-stable the oracle is unfalsifiable.*
3. **`GraphStore` on SQLite** (better-sqlite3, WAL) — the `memo` / `deps` (+ `rdeps` index) / `inputs` / `meta` schema of §4, plus `putNode`/`getNode`/`putDeps`/`getRdeps`/`txn`/`snapshot`.
4. **The determinism harness** — round-trip a hand-authored `PartialGraph` to a golden snapshot; assert an identical digest across 100 runs, shuffled insertion order, a store teardown/reload, and a second OS. Wire it as a blocking CI gate.

**P0 exit = that harness is green.** No engine, no parser yet — but the foundation the entire moat stands on is proven bit-stable first.
