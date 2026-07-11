# bkg — Backend Knowledge Graph

A **local-first, incrementally-updated, provenance-tracked knowledge graph of a backend codebase**, served to AI coding agents over MCP with **no LLM on the query path**. It understands routes/endpoints, DTOs, middleware, auth, DB models, and data flow via deterministic static analysis — and keeps the graph current by recomputing **only the facts whose value actually changed** when code changes.

> **First target:** Python **FastAPI** projects. The engine is written in Python.

## Why it's different (the moat)

Not "a code graph over MCP" (already commoditized). The defensible combination is:
1. **Backend semantic depth** — auth / DTO fields / middleware chains / env / queue / cron / data flow, not just route→handler.
2. **Universal per-fact provenance + confidence** — every node/edge records `source`, `confidence`, and `verificationStatus`.
3. **Verified true-incremental correctness** — recompute only value-changed facts via reverse-dependency propagation + early cutoff, proven by a **byte-identical rebuild oracle**.

See [docs/bkg-kg-core-build-plan.md](docs/bkg-kg-core-build-plan.md) (build plan), [docs/bkg-research-incremental-code-kg.md](docs/bkg-research-incremental-code-kg.md) (prior-art research), and [docs/updated-plan.md](docs/updated-plan.md) (full design).

## Stack
Python 3.12 · pydantic v2 (protocol models) · BLAKE3 (content-addressed fingerprints) · SQLite (via the stdlib `sqlite3`, **behind the `GraphStore` port**) · pytest + hypothesis (determinism harness). Later: py-tree-sitter (structure), LibCST / Jedi / Pydantic introspection (depth), the Python MCP SDK.

## Layout
```
src/bkg/
  protocol/   frozen node/edge/IR/enum vocabulary + canonical serializer + BLAKE3 digest
  store/      GraphStore PORT (db-agnostic) + SqliteGraphStore (the ONLY db-aware module)
  snapshot.py materialize a PartialGraph -> memo rows; canonical whole-graph snapshot/digest
tests/        the determinism harness + store round-trip
docs/         design, research, build plan
```

## Build, test
```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run mypy
```

## Status
**P0 — foundation.** Frozen protocol + canonical serializer + BLAKE3 + `GraphStore` port + SQLite store + the determinism harness (byte-identical snapshot across repeats / shuffled insertion / reload). The incremental engine + oracle (P1) come next. No parser yet.
