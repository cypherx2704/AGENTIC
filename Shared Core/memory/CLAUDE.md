# CLAUDE.md — memory-service (Shared Core/memory)

> Principal-scoped agent memory for the CypherX platform (Phase 6 / WP10): store, vector-search, by-id CRUD, sessions, and GDPR bulk-wipe over pgvector, with embeddings via the llms-gateway and a deterministic offline fallback. Platform root guide: ../../CLAUDE.md. Contracts are the single source of truth: ../../contracts/.

## What this is
A fully-implemented Python 3.12 / FastAPI service that gives agents durable memory. A memory is owned by a `(principal_type, principal_id)` pair resolved from the JWT (agent or on-behalf-of user). It stores content + a 1536-dim embedding, searches by cosine similarity (pgvector HNSW), enforces per-principal quotas, isolates tenants via Postgres RLS, and emits audit/usage events through a transactional outbox to Kafka. Implements phase-06-memory (`../../archive/Manoj/phases/phase-06-memory.md`) — see "Gotchas" for where the code intentionally simplifies that spec.

## Tech stack
- **Language/runtime:** Python 3.12; FastAPI + uvicorn (ASGI).
- **Build:** uv (`uv.lock`, frozen installs) + hatchling; package `memory_service` under `src/`. Lint: ruff (line-length 110).
- **Data:** psycopg3 async pool (`psycopg[binary,pool]`) + pgvector (HNSW cosine); Pydantic v2 / pydantic-settings.
- **Infra libs:** redis/redis.asyncio (Valkey), aiokafka (outbox publisher), PyJWT[crypto] (JWKS / RS256), structlog (JSON logs), prometheus-client.
- **Tests:** pytest + pytest-asyncio (auto mode), asgi-lifespan, respx. 11 `test_*` modules, all deterministic/offline.

## Repository layout
| Path | Holds |
|------|-------|
| `src/memory_service/main.py` | App factory + lifespan: DB pool, repo selection, embedder, Valkey, outbox publisher, TTL sweep, JWKS warm. |
| `src/memory_service/__main__.py` | `python -m memory_service` entry (uvicorn; Windows SelectorEventLoop shim for psycopg3 async). |
| `src/memory_service/api/` | Routers: `memories.py` (store/search/by-id), `sessions.py`, `gdpr.py`, `health.py` (`/livez` `/readyz` `/metrics`). |
| `src/memory_service/core/` | `auth.py` (dual-mode JWT + revocation), `errors.py` (Contract-2), `config.py` (settings), `logging.py`, `trace.py`, `metrics.py`. |
| `src/memory_service/services/` | `repository.py` (abstract + in-memory impl), `pg_repository.py` (Postgres), `embeddings.py`, `quota.py`, `idempotency.py`, `scoping.py` (visibility predicate), `similarity.py`. |
| `src/memory_service/db/` | `pool.py` (`in_tenant` RLS tx helper), `outbox.py` (Contract-5 envelope + publisher), `valkey.py`. |
| `src/memory_service/models/memory.py` | Pydantic wire models (`extra="forbid"`). |
| `db/migrations/20260611_0001__init.sql` | Idempotent schema: `memory` schema, RLS, grants to `mem_user`, pgvector + pgcrypto, `pricing` seed. |
| `tests/` | `conftest.py` + `_helpers.py` harness; behavior tests (scopes, dedup/idempotency, gdpr, quota, ttl/isolation, etc.). |
| `Dockerfile`, `.env.example`, `pyproject.toml`, `uv.lock` | Multi-stage uv image; example env; manifest + lockfile. |

## Build, test, run
```bash
python -m uv venv
python -m uv sync
# Tests (deterministic; no live infra — mock embedder, in-memory repo, FakeValkey, db_pool=None):
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider -o faulthandler_timeout=60
./.venv/Scripts/python.exe -m ruff check .
# Run locally (honours HOST/PORT):
python -m memory_service          # defaults 0.0.0.0:8000 when PORT unset
# Docker (image sets PORT=8080, non-root uid 10001, stdlib HEALTHCHECK on /livez):
docker build -t cypherx/memory-service .
docker run -p 8080:8080 cypherx/memory-service
```
- **infra/compose** (`infra/compose/docker-compose.yml`, service `memory`, build context `../../Shared Core/memory`): in-container **8080**, host map **8088:8080**; env sets `PORT=8080`, `EMBEDDINGS_BASE_URL=http://llms-gateway:8080`, `KAFKA_BROKERS=redpanda:29092`, `VALKEY_URL=redis://valkey:6379`, mocks on; `depends_on` redpanda/valkey/auth-service healthy. Compose healthcheck hits `/livez`. Migrations mounted into the platform `migrate` job (`db/migrations` → `/migrations/memory`).
- **Health:** `GET /livez` (process-only, never touches deps), `GET /readyz` (200/503; gated ONLY on Postgres — Valkey reported `ok|unavailable` but never fails readiness), `GET /metrics` (Prometheus 0.0.4 text).
- **Endpoints:** `POST /v1/memories`, `POST /v1/memories/search`, `GET|PUT|DELETE /v1/memories/{id}`, `POST /v1/sessions`, `POST /v1/gdpr/wipe`.

## Configuration & secrets
Env is read with **no prefix** (Doppler convention). Only `.env.example` is committed — never a real `.env`. Full set + defaults in `core/config.py`.
- `DATABASE_URL` — Postgres DSN (external Neon; runtime role `mem_user`, `memory` schema). No postgres container.
- `KAFKA_BROKERS`, `VALKEY_URL` — Redpanda + Valkey (containers locally).
- `AUTH_JWKS_URL` / `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE` — Contract-1 JWT verification (RS256; aud accepts the platform audience OR `*` for service tokens).
- `EMBEDDINGS_BASE_URL` / `EMBEDDINGS_MODEL` (`embed`) / `EMBEDDINGS_SERVICE_TOKEN` — llms-gateway `POST /v1/embeddings`. Default base `:8081` collides with Redpanda SR; compose overrides to `llms-gateway:8080`.
- **Mock toggles (keyless local):** `EMBEDDINGS_MOCK_FALLBACK=true` OR `MOCK_PROVIDERS=true` → `use_mock_embeddings` → always-on deterministic SHA-256 embedder, no network. When off, gateway is tried first and the mock is the fail-open fallback.
- `CONTENT_MAX_BYTES` (16384), `DEDUP_THRESHOLD` (0.95), `SEARCH_TOP_K_MAX` (50), `TTL_SWEEP_*`, `QUOTA_*` (`DEFAULT_PLAN=free`), `REVOCATION_*`.

## Contracts & cross-repo dependencies
- **Implements:** Contract-2 error envelope (`core/errors.py`; note `IDEMPOTENCY_*` spelling, not `IDEMPOTENT_*`); Contract-5 event envelope (`db/outbox.py`, `partition_key = tenant_id`); Contract-6 JSON logging (`event`→`message`); Contract-7 health; Contract-8 W3C trace context (`core/trace.py`); Contract-13 tenant RLS; Contract-19 quota (`services/quota.py` + `memory.pricing`).
- **Auth (Contracts 1/12/13):** EXTERNAL bare agent/api-key JWT, or INTERNAL service JWT (`sub=svc:*`/`svc-ext:*`) + `X-Forwarded-Agent-JWT` where service `on_behalf_of` MUST equal the forwarded agent's `agent_id`. Identity comes ONLY from the JWT. Requires a `mem:read`/`mem:write` scope (403 otherwise). WP03 verifier-side revocation mirror (Valkey jti/kid/agent-epoch, fail-open) runs after signature/iss/aud/exp/scope.
- **Calls:** llms-gateway (`POST /v1/embeddings`) for vectors; auth-service JWKS for verification.
- **Called by:** xAgent MEMORY_WRITE stage hits `POST /v1/memories` directly (async fire-and-forget, service JWT + forwarded agent JWT) — there is no Kafka memory-write topic (`cypherx.memory.write.requested` was deleted in the spec).
- **Kafka produced (via outbox):** `cypherx.memory.stored`, `cypherx.memory.deleted`, `cypherx.memory.gdpr.wiped` (+ `<topic>.dlq` after 10 attempts). Consumes none.
- **DB owned:** schema `memory` (role `mem_user`): `tenant_config`, `memories`, `memory_vectors_1536`, `sessions`, `gdpr_wipe_log`, `outbox`, `pricing`.

## Invariants & guards (do NOT break)
- **Cross-end-user leak guard:** `principal_only` memories are NEVER visible to another principal, under any policy. `tenant_shared` crosses only when tenant `user_scope_visibility = 'tenant'` (default `isolated`). The single predicate lives in `services/scoping.can_view` and is mirrored EXACTLY by the SQL in `pg_repository` (search + by-id); keep them identical.
- **404, never 403** for a memory the caller cannot see (anti-existence-leak) — by-id GET/PUT/DELETE return NOT_FOUND on invisible/non-owned rows. Mutation/delete are owner-only.
- **Idempotency-Key short-circuits BEFORE embedding** — replay must never re-embed (tests assert embed-call count). Keys namespaced by owning principal. In-flight → 409.
- **Dedup ≥ threshold → bump-only** — a near-duplicate (same principal, nearest cosine neighbour) bumps `last_accessed_at` + `score+1` instead of inserting; content/embedding never replaced.
- **RLS:** every tenant-scoped query runs through `db/pool.in_tenant` (sets transaction-local `app.tenant_id`). `mem_user` is not superuser and does NOT bypass RLS; tenant isolation is enforced by the DB. The `outbox` table has RLS DISABLED (drained cross-tenant by the publisher); isolation is in the payload. The TTL sweep + outbox drain run on raw pool connections WITHOUT `app.tenant_id` (cross-tenant batch ops).
- **Fail-open:** quota/rate, the revocation mirror, idempotency, and the embeddings gateway all fail open (availability wins). Resource caps (`memories_max`/`storage_bytes_max`) are the exception — hard ceilings checked against live COUNT/SUM, and skipped only when there's no DB.
- **Identity from JWT only** — request models forbid `tenant_id`/`principal_id` fields; `extra="forbid"` on all wire models.
- **Atomicity:** store(row+vector+event), delete(+event), GDPR(log+delete+sessions+event) each run in ONE tenant transaction; outbox `emit` writes on the caller's connection.
- **Vector dim is fixed at 1536.** Content cap 16 KiB → 413 VALIDATION_ERROR. `top_k` clamped to `SEARCH_TOP_K_MAX` (50); search is a two-pass ANN CTE (oversample ×4).

## Gotchas & current status
- **Status: fully implemented** for the WP10 first-cycle surface. Not a stub.
- **Port default mismatch:** `__main__.py` defaults `PORT=8000`; the Docker image and compose set `PORT=8080`. Canonical in-container port is 8080 — run with PORT set.
- **Intentional simplification vs phase-06 spec:** the code uses a 2-value memory `scope` (`principal_only` | `tenant_shared`) + tenant `user_scope_visibility` (`isolated` | `tenant`, default `isolated`), and routes `/v1/memories/search`, `/v1/sessions`, `/v1/gdpr/wipe`. The spec instead describes a 5-value scope enum (`tenant`/`principal`/`agent`/`user`/`session`) with a `scope_id` UUID column and an `importance` field, routes `/v1/memories/retrieve`, `/v1/memories/sessions`, `/v1/memories/extract`, `/v1/memories/summarise`, query-param GDPR `DELETE /v1/memories?scope=&scope_id=`, and `user_scope_visibility` values `principal_only|tenant_shared`. None of those spec-only shapes are implemented. Treat the code on disk as authoritative.
- **Single migration** (`20260611_0001__init.sql`); no Atlas/versioned migration tooling here. HNSW index has no explicit `WITH (m, ef_construction)`; `ef_search`/`min_score` knobs from the spec are not implemented.
- **Degraded mode:** if the DB pool can't open at boot, the lifespan swaps in `InMemoryRepository` (logs `memory_repo_degraded_in_memory`); `/readyz` then reports DB fail. The test harness uses this same in-memory repo with `db_pool=None`, so resource-cap reads and the outbox publisher no-op in tests.
- **Search `similarity`** is surfaced as a clamped/remapped score `(cos+1)/2` in `[0,1]` on the wire; the in-memory repo computes cosine in Python, the PG repo uses `1 - (<=>)`.
- **Shipped & flag-gated (research-backed, additive — defaults preserve today's pure-cosine behaviour; verified 2026-06-14):** retrieval scoring (`memory_scoring_enabled`=false → composite recency+importance+relevance, Stanford "Generative Agents"; `…weight_recency/importance/relevance`=1.0, `…recency_half_life_seconds`=7d), LLM-graded importance on write (`memory_importance_llm_enabled`=false — DEFAULT-OFF skeleton), contradiction / temporal validity supersession (`memory_contradiction_enabled`=false, `…sim_min`=0.80), consolidation+forgetting job (`memory_consolidation_enabled`=false — DEFAULT-OFF skeleton), and the Contract-19 `cypherx.memory.usage.recorded` outbox event (`memory_usage_events_enabled`=**true** by default). The importance-grader, consolidation clusterer/summary, and contradiction detector ship as default-off skeletons (heuristic/placeholder bodies); real provider wiring is deferred.
- **Not built (📋 in spec):** auto-extraction, summarisation, working memory, `user_scope_acl`, re-embed job, pluggable vector backends, async `last_accessed_at` batching (it writes inline per retrieve via `UPDATE ... WHERE id = ANY(...)`).
- Sessions: `session_id` is the GLOBAL PK today. `create_session` catches a UNIQUE violation and maps it to **409 `SESSION_PRINCIPAL_COLLISION`** (never an unhandled 500) — covering both the same-tenant create race and the cross-tenant global-PK collision (where RLS hides the owning row from the pre-INSERT SELECT). A STAGED migration `20260614_0003__sessions_tenant_scoped_pk.sql` re-keys the table per-tenant `(tenant_id, session_id)`; apply it via the migrate job to also close the minor cross-tenant existence side-channel. A different principal reusing an id → 409; `sessions.create` is idempotent for the same principal.
