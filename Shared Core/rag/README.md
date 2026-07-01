# CypherX RAG service

> Phase 5 / WP09 — the universal retrieval-augmented generation service. Any agent can create
> knowledge bases, ingest documents, and retrieve relevant context chunks (pgvector semantic
> search) before calling an LLM. Built from the shared SharedCore template (auth/config/errors/
> logging/metrics/trace/outbox/RLS), Python 3.12 + FastAPI + uv.

## What it does

- **Knowledge bases** — create/get/list/delete `/v1/kbs`. The requested embedding alias is
  resolved to a literal model id + dim at creation and stored **immutably**; a default
  `(tenant,'*')` ACL row is written in the same transaction (unless `private`).
- **Query** `POST /v1/kbs/{id}/query` — embed the query, run a two-pass HNSW-friendly CTE
  (`SET LOCAL hnsw.ef_search`), `top_k` cap 100, ACL check (403 `FORBIDDEN_KB` on deny).
- **Inline ingest** `POST /v1/kbs/{id}/documents` (≤100 KiB text) — fixed/sentence chunking,
  batched embeddings with deterministic Idempotency-Keys, batched vector INSERTs.
- **Presigned upload + finalize** — SigV4 presigned PUT (env-driven SSE mode for MinIO);
  finalize HeadObjects the key and enqueues ingestion via the outbox (Idempotency-Key replay).
- **Kafka ingestion worker** — consumes `cypherx.rag.ingestion.requested`, chunks + embeds +
  stores, with poison-pill retry (3 attempts, backoff) then DLQ + `ingestion.failed`.
- **Usage metering** — units + `request_id` only (Contract-14 single-owner) to the outbox.
- **Quota enforcement** — `kbs_max` / `documents_per_kb_max` / `queries_per_min` /
  `storage_bytes_max` from the JWT plan/limits; 413/429 on breach; fail-open if unresolved.
- **Platform-skills bootstrap** — lazy-with-retry loop seeds the `platform-skills` KB + its
  default ACL under the platform tenant; `/readyz` gates only on the loop *running* (no live
  llms call) so cold start can't deadlock on the llms soft dependency.
- **IVectorStore + PgVectorAdapter** — the backend is swappable; pgvector is the only impl.

## Endpoints

| Method | Path | Scope | Notes |
|--------|------|-------|-------|
| POST | `/v1/kbs` | `rag:admin` | create KB (alias resolved + immutable; default ACL) |
| GET | `/v1/kbs` / `/v1/kbs/{id}` | `rag:query`/`rag:admin` | list / get |
| GET | `/v1/kbs/{id}/status` | `rag:query` | counts computed on demand |
| DELETE | `/v1/kbs/{id}` | `rag:admin` | platform-skills KB is non-deletable |
| POST | `/v1/kbs/{id}/query` | `rag:query` | two-pass CTE; 403 `FORBIDDEN_KB` |
| POST | `/v1/kbs/{id}/documents` | `rag:ingest` | inline ingest (≤100 KiB) |
| POST | `/v1/kbs/{id}/documents/upload-url` | `rag:ingest` | presigned PUT |
| POST | `/v1/kbs/{id}/documents/finalize` | `rag:ingest` | idempotent enqueue (202) |
| GET | `/v1/kbs/{id}/documents` / `/{doc_id}` | `rag:query` | list / status |
| DELETE | `/v1/kbs/{id}/documents/{doc_id}` | `rag:ingest` | DB cascade + s3_deletions queue |
| GET/POST/PUT/DELETE | `/v1/kbs/{id}/acls...` | `rag:admin` | KB ACL management |
| GET | `/livez` `/readyz` `/metrics` | — | Contract 7 |

All errors render the Contract-2 envelope. Identity (tenant/agent) is derived from the JWT
chain only — request bodies that carry identity fields are rejected (`extra="forbid"`).

## Auth modes

- **EXTERNAL** — bare agent JWT in `Authorization` (verified directly against Auth JWKS).
- **INTERNAL** — service JWT (`sub=svc:*`) + `X-Forwarded-Agent-JWT`; `on_behalf_of` must
  match the forwarded agent's `agent_id`.
- **CROSS-TENANT PLATFORM READ** — a service JWT carrying the platform tenant + `on_behalf_of`
  + `internal:read` (Skills → `platform-skills`). RAG RLS stays strict single-tenant; the
  token sets the platform tenant context, never a mid-request swap.

The shared WP03 verifier-side revocation mirror (jti/kid/agent-epoch) runs after verification
and **fails open** when Valkey is unavailable.

## Embeddings (mock fallback)

Embeddings go through the llms-gateway `POST /v1/embeddings` with a Contract-12 service token.
A deterministic SHA-256-seeded, L2-normalized **mock vector** (byte-identical to the gateway's
mock provider) is used when `MOCK_EMBEDDINGS=true` *or* when a real call fails and
`EMBEDDINGS_FALLBACK_TO_MOCK=true` — so tests + offline local need no network. Vector dim 1536.

## Run

```bash
python -m uv venv && python -m uv sync
# API server
PORT=8000 MOCK_EMBEDDINGS=true \
  DATABASE_URL=postgresql://rag_user:localdev@localhost:5432/cypherx_platform \
  PGOPTIONS='-c search_path=rag,public' \
  .venv/Scripts/python.exe -m rag_service          # (Linux: .venv/bin/python)
# Ingestion worker (same env + RAG_RUN_WORKER=1)
RAG_RUN_WORKER=1 ... .venv/Scripts/python.exe -m rag_service
```

Migrations + schema details: `db/migrations/README.md`.

## Tests

In-process, no Postgres/Valkey/Kafka/llms — an in-memory fake pool (`tests/fakes.py`) answers
the exact SQL and records every `app.tenant_id` (RLS-isolation assertions); the mock embedder
gives deterministic vectors.

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -o faulthandler_timeout=60
.venv/Scripts/python.exe -m ruff check src tests
```
