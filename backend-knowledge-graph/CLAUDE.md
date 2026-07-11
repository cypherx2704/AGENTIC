# CLAUDE.md — bkg (Backend Knowledge Graph)

> A **local-first, incrementally-updated, provenance-tracked** knowledge graph of a **backend codebase** (routes/DTOs/middleware/auth/DB/data-flow), served to AI agents over **MCP with NO LLM on the query path**. Deterministic static analysis is the source of truth; runtime observation and a strictly-capped AI proposer are later layers. First target: **Python FastAPI**. Full design in [docs/updated-plan.md](docs/updated-plan.md); build plan in [docs/bkg-kg-core-build-plan.md](docs/bkg-kg-core-build-plan.md); prior-art research in [docs/bkg-research-incremental-code-kg.md](docs/bkg-research-incremental-code-kg.md).

## What this is (and is NOT)
A **separate product** from `CoreProjects/cypherx-a1/` (the Python engineering-memory service). Different graph (code-structural, not social/temporal), different shape (local CLI+MCP, not a cloud service). Do not mix them.

## The moat (three things, only these)
1. **Backend semantic DEPTH** — auth / DTO fields / middleware chain / env / queue / cron / data flow, not just route→handler.
2. **Universal per-fact provenance + confidence** on every node/edge.
3. **VERIFIED true-incremental correctness** — recompute only value-changed facts via reverse-dep propagation + early cutoff, proven by a **byte-identical rebuild oracle**. (Everyone else does watch→re-parse-changed-file; that is not a moat.)

## Load-bearing invariants (do NOT break)
- **No LLM on the query path.** All reasoning is build-time/deterministic; queries are pure index lookups.
- **The database is hidden behind the `GraphStore` port.** `src/bkg/store/sqlite_store.py` is the ONLY module that imports a DB driver. Everything else uses the `GraphStore` ABC + `open_store()` factory. Swapping stores later = one new class, zero changes elsewhere.
- **`@bkg/protocol` (src/bkg/protocol) is the frozen vocabulary.** Nodes/edges/IR/enums + the canonical serializer. `ai`/`runtime` confidence tiers + `verification_status` are declared but UNUSED in the core.
- **Canonical serialization must be bit-stable** — deterministic key ordering, no wall-clock, repo-relative POSIX paths only, no floats. The whole determinism oracle reduces to `digest(incremental) == digest(rebuild)`.
- **Stable nominal identity** — keys are `route:{file}:{router}:{method}:{path}`, `handler:{file}#{symbol}`, etc. — NEVER byte offset / array index.
- The incremental engine (P1) is built **before** the real parser. Retrofitting incrementality onto a finished assembler collapses true-incremental (III) into re-parse-changed-file (II).

## Build / test / run
```bash
uv sync
uv run pytest                     # determinism harness must be green
uv run ruff check src tests
uv run mypy
```

## Layout
| Path | Holds |
| --- | --- |
| `src/bkg/protocol/enums.py` | `Confidence` / `Provenance` / `VerificationStatus` / `NodeKind` / `EdgeKind` / `HttpMethod`. |
| `src/bkg/protocol/models.py` | Frozen pydantic node/edge/IR models + the `PartialGraph` container. |
| `src/bkg/protocol/canonical.py` | `canonical_bytes()`, `fingerprint()` (BLAKE3 raw), `hexdigest()`. Pure, no store/DB import. |
| `src/bkg/store/base.py` | The `GraphStore` **port** (ABC) + `MemoRow` / `Dep` / `InputRow`. |
| `src/bkg/store/sqlite_store.py` | `SqliteGraphStore` — the memo/deps/inputs/meta schema; the ONLY DB-aware module. |
| `src/bkg/store/__init__.py` | `open_store(path)` factory — callers never name the backend. |
| `src/bkg/snapshot.py` | `materialize(PartialGraph)→MemoRow[]`, `rows_digest`, `snapshot_bytes/digest(store)`, `load(store, rows)`. |
| `src/bkg/engine.py` | The demand-driven memoized incremental engine: `define_query` / `query` / `set_input` / `remove_input` / `snapshot_digest` / `dep_map`. |
| `src/bkg/adapters/fastapi.py` | The FastAPI adapter — stdlib-`ast` `extract(source)` → file-local routes/mounts/imports + `resolve_target` (import resolution). Deterministic, file-local. |
| `src/bkg/pipeline.py` | The real pipeline: memoized queries (`fileFacts`→projections→`allMounts`/`mountChain`→`endpoint`→`graph:all`) + `apply_sources`. Inputs: `fileText:{path}`, `files:all`. |
| `src/bkg/service.py` | `GraphService` — the RPC surface (`list_endpoints` / `get_endpoint` / `apply_change`) + `load_directory` (reads `.py`, utf-8-sig BOM-safe). No LLM on the query path. |
| `src/bkg/cli.py` | The `bkg` CLI (`endpoints` / `endpoint`) — a thin transport over `GraphService`. |
| `src/bkg/mcp_server.py` | The `bkg-mcp` MCP server (FastMCP) exposing `list_endpoints` / `get_endpoint` tools — no LLM on the query path. |
| `tests/` | determinism harness (P0), engine + oracle (P1, `synthetic.py`), canonical rejects, FastAPI adapter + real-source pipeline integration. |

## Status & next
- **P0 — DONE.** Frozen protocol + canonical serializer (BLAKE3, length-prefixed snapshot framing) + `GraphStore` port + SQLite store + determinism harness.
- **P1 — DONE (moat proof).** The demand-driven memoized engine ([src/bkg/engine.py](src/bkg/engine.py)): global revision, `cx.read` dep capture (de-duplicated), **try-mark-green + backdating early cutoff**, reachability-based snapshots, `remove_input` reverse-dep-closure invalidation, structural input/derived classification (reload-safe), cycle detection. The determinism oracle ([tests/test_incremental_oracle.py](tests/test_incremental_oracle.py)) asserts, over random + targeted edit sequences: **byte-identical vs rebuild + dependency-edge equality + idempotence** (a redundant re-query recomputes nothing) + zero-cascade + sibling-isolation. Hardened by an adversarial-review pass (fixed: rdep invalidation on delete, input aliasing, zero-dep vacuous-green, duplicate-read PK crash, commit-order drift, float/NaN/tuple canonical leaks). **29 tests, ruff + mypy clean.** Synthetic pipeline only — no real parser yet.
- **P2/P3 core — DONE.** The graph is now built from **real Python/FastAPI source** (not the synthetic pipeline): the stdlib-`ast` FastAPI adapter ([src/bkg/adapters/fastapi.py](src/bkg/adapters/fastapi.py)) + the real pipeline ([src/bkg/pipeline.py](src/bkg/pipeline.py)) assemble correct Endpoints with **cross-file `include_router` mounting** resolved at query time (absolute + relative imports). Integration tests prove it stays incremental on real source edits — a **comment edit re-parses one file and cascades to nothing**, a route edit touches only its endpoint, a mount-prefix edit re-resolves all endpoints under it, all byte-identical to a rebuild. **43 tests, ruff + mypy clean.**
- **P3 serving layer — DONE.** `GraphService` RPC surface + the `bkg` **CLI** (`endpoints` / `endpoint`) + the `bkg-mcp` **MCP server** (FastMCP tools, no LLM on the query path), all over the incremental graph. Verified end-to-end on a real on-disk FastAPI project (incl. BOM-encoded Windows files). **55 tests, ruff + mypy clean.**
- **Next — P4 (the ~50% moat wedge): backend semantic DEPTH** — Pydantic DTO fields + `response_model` on the Endpoint (`body`/`response` SchemaRef + Field IR), then `Depends`/auth into `GUARDED_BY`/`middleware_chain`. All in-process via `ast` + Pydantic introspection. Also pending: per-fact provenance/confidence surfaced on served payloads, and `blast_radius` over the rdep closure.
