# CLAUDE.md — cypherx-a1 (CoreProjects/cypherx-a1)

> CypherX **Autonomous Engineering Memory**: ingests engineering sources (GitHub first) into a tenant-scoped knowledge **graph** + **RAG** corpus, runs an LLM knowledge-extraction pass, and serves a **cited hybrid-retrieval copilot** plus a stateless **MCP server** (`mcp-eng-memory`) so AI coding agents can ask "who built what / who owns this / what breaks if I change X / why was this decided". A first-class CypherX **consuming app** (peer of `xAgent/ax-1`) — NOT a SharedCore service. Platform root guide: [../../CLAUDE.md](../../CLAUDE.md). Full design: [docs/](docs/).

## What this is
The product turns scattered engineering history into a queryable memory. It is a **layered combination**: (A) a FastAPI **product service** owning ALL domain logic + the `cypherx_a1` Postgres schema, (B) a **copilot** that calls llms-gateway + guardrails directly, and (C) a **separate stateless MCP facade** (`mcp-eng-memory/`) that proxies (A)'s query API over the MCP contract. It **reuses the SharedCore services** (auth, llms, guardrails, rag, memory) strictly through their versioned `/v1` contracts and pushes **no business logic into SharedCore**. **Status: MVP implemented** (Phase 0 foundations + Phase 1 GitHub-first slice + Phase 2 MCP server). The async Kafka worker is a documented seam (ingestion/extraction run synchronously via the authenticated API for the MVP).

## Tech stack
- **Python 3.12 / FastAPI / uv** (hatchling, package `cypherx_a1`; entrypoint `python -m cypherx_a1`). Mirrors the xAgent ax-1 / SharedCore Python service template.
- **pydantic v2** + pydantic-settings (env config, no prefix, Doppler-compatible).
- **psycopg3** async (`psycopg[binary,pool]`) — RLS, runtime role `cxa1_user`. **aiokafka** (outbox drain). **redis** → Valkey (soft, revocation mirror only). **pyjwt[crypto]** (RS256 JWKS). **httpx** (SharedCore clients). **structlog**, **prometheus-client**.
- Tests/lint: pytest, pytest-asyncio (`asyncio_mode=auto`), respx, asgi-lifespan; ruff (line-length 110), mypy.
- `mcp-eng-memory/` is its own lean package (no DB/Kafka deps).

## Repository layout
| Path | Holds |
| --- | --- |
| `src/cypherx_a1/main.py` | App factory + lifespan: DB pool, service-token provider + SharedCore clients, retrieval orchestrator, copilot + graph-query services, outbox publisher, JWKS warm. |
| `src/cypherx_a1/api/` | Routers: `health`, `copilot` (`POST /v1/copilot/ask`), `graph` (`/v1/graph/*`), `connectors` (`/v1/connectors/{kind}/sync`, `/v1/extract`), `webhooks` (`POST /webhooks/{kind}`). |
| `src/cypherx_a1/core/` | `config`, `auth` (JWKS verify + revocation mirror + scopes), `errors` (Contract-2), `trace` (Contract 8 + OTel opt-in), `logging` (Contract 6), `metrics`. |
| `src/cypherx_a1/db/` | `pool` (`in_tenant` RLS tx), `outbox` (Contract-5 envelope + publisher), `graph_repo` (entities/edges upsert + recursive-CTE reads), `ingest_repo` (landing/cursors/extraction-ledger/citations/kb-bindings). |
| `src/cypherx_a1/models/` | `canonical` (the unified ingestion model), `api` (wire models + `Citation`, `extra="forbid"`). |
| `src/cypherx_a1/connectors/` | `base` (SPI), `github` (connector + keyless fixtures), `registry`. |
| `src/cypherx_a1/ingestion/` | `normalizer` (canonical→graph + identity resolution), `pipeline` (landing→graph→RAG→citation, `KbResolver`). |
| `src/cypherx_a1/extraction/` | `extractor` (LLM json_object extraction, idempotent, bitemporal supersede, cost-metered). |
| `src/cypherx_a1/retrieval/` | `orchestrator` (graph + RAG-dense + tsvector, **RRF fusion**, cited). |
| `src/cypherx_a1/copilot/` | `queries` (read-only graph tools), `service` (the cited copilot flow). |
| `src/cypherx_a1/services/` | SharedCore clients: `service_token`, `llms_client`, `guardrails_client`, `rag_client`, `memory_client`, `valkey`. |
| `src/cypherx_a1/worker/` | `runner` (async ingestion/extraction Kafka worker — documented seam). |
| `db/migrations/` | Atlas: `20260614_0001__init.sql` (schema + roles + RLS), `_0002__seed.sql` (auth.service_acl edges), `schema.sql`, `atlas.hcl`. Schema `cypherx_a1`. |
| `mcp-eng-memory/` | The stateless MCP server (its own package `mcp_eng_memory`, `manifest.json`, Dockerfile, pyproject). |
| `docs/` | The product-development documentation set (00–17 + ADRs). |
| `Dockerfile`, `pyproject.toml`, `.env.example`, `openapi.yaml`, `README.md` | Manifests + image + example env + published spec. |

## Build, test, run
**Host (uv):**
```bash
uv sync
export SERVICE_BOOTSTRAP_SECRET=local-dev-cypherxa1-secret   # required, no default (fails fast)
uv run uvicorn cypherx_a1.main:app --reload --port 8093
uv run pytest            # network-free
uv run ruff check src tests && uv run mypy
```
**Docker / infra/compose** (service `cypherx-a1`, build `CoreProjects/cypherx-a1`, host **8093→8080**; `mcp-eng-memory` host **8094→8080**):
- Migrate first: `docker compose --profile migrate up migrate` (applies `db/migrations/*__init.sql` then `_0002__seed.sql` against Neon DIRECT; creates schema `cypherx_a1` + role `cxa1_user`, seeds the `auth.service_acl` edges).
- Bring up: `docker compose up -d --build cypherx-a1 mcp-eng-memory` (deps → auth → llms+guardrails → rag+memory → cypherx-a1 → mcp-eng-memory).
- Keyless: `CONNECTOR_MODE=mock` (bundled GitHub fixtures) + upstream `MOCK_PROVIDERS`/`MOCK_EMBEDDINGS`.

**Health (Contract 7):** `/livez` (process-only), `/readyz` (Postgres + warm Auth JWKS), `/metrics`.

## Configuration & secrets
Env via pydantic-settings (no prefix; Doppler-injected; `core/config.py` authoritative). Key vars: `SERVICE_BOOTSTRAP_SECRET` (**required, no default**; compose source `SERVICE_BOOTSTRAP_SECRET_CYPHERXA1`), `DATABASE_URL` (role `cxa1_user`, Neon POOLED; compose source `CYPHERXA1_DATABASE_URL`), `AUTH_JWKS_URL`/`AUTH_ISSUER_URL`/`AUTH_PLATFORM_AUDIENCE`/`AUTH_SERVICE_URL`, `LLMS_GATEWAY_URL`/`GUARDRAILS_SERVICE_URL`/`RAG_SERVICE_URL`/`MEMORY_SERVICE_URL`, `RAG_EMBEDDING_MODEL` (pinned), `CONNECTOR_MODE` (`mock`|`live`) + `GITHUB_TOKEN`/`GITHUB_WEBHOOK_SECRET`, `KAFKA_BROKERS`, `VALKEY_URL` (soft). `mcp-eng-memory` adds `CYPHERXA1_BASE_URL`, `MANIFEST_PATH`. Only `.env.example` is committed.

## Contracts & cross-repo dependencies
- **Implements/honours** (`../../contracts`): Contract 1 (JWKS RS256 verify), 2 (error envelope), 5 (Kafka envelope, `partition_key=tenant_id`, `cypherx.cypherxa1.*` topics + `.dlq`), 6 (structlog JSON), 7 (health), 8 (W3C trace), 9 (Idempotency-Key on llms/rag/memory writes), 12 (service-token + `X-Forwarded-Agent-JWT`), 13 (tenant RLS), 14 (Atlas migrations), 19 (usage metering — units + request_id, never rewrite the gateway's cost), **4 (MCP manifest** for `mcp-eng-memory`, validated against `contracts/mcp/manifest.schema.json`).
- **Calls downstream** (identity in HEADERS only): Auth (service-token mint, JWKS), llms-gateway (`/v1/chat/completions`, `/v1/embeddings`), guardrails (`/v1/check/input|output`), rag (`/v1/kbs`, `/v1/kbs/{id}/documents`, `/v1/kbs/{id}/query`), memory (`/v1/memories[/search]`, `/v1/sessions`).
- **DB schema owned**: `cypherx_a1` (entities/edges/identities/raw_events/connectors/connector_secrets/sync_cursors/extraction_jobs/citations/resource_acls/rag_kbs — RLS) + `outbox` (NO RLS). Role `cxa1_user` (LOGIN, no BYPASSRLS, no CREATE EXTENSION).
- **Kafka produced** (via outbox): `cypherx.cypherxa1.record.normalized`, `cypherx.cypherxa1.usage.recorded` (+ `.dlq`). Consumes none in the MVP.

## Invariants & guards (do NOT break)
- **Identity from JWT only** (Contract 13): `tenant_id`/`agent_id` come from the verified token, never a body. Request models use `extra="forbid"`.
- **The GRAPH is the crown jewel and is APP-OWNED.** It never enters RAG (`rag.chunks` are opaque text+metadata) and never enters Memory (per-principal; cross-principal leakage + embedding-cost). A code-review/lint guard keeps the corpus out of Memory.
- **RAG vectors only; pinned embedding model.** KBs are created with an explicit model name (never the repointable `embed` alias); resolved model+dim are persisted immutably in `rag_kbs`.
- **Hybrid retrieval is app-side.** RAG ships dense-only first cycle — keyword (tsvector), RRF fusion, rerank, query expansion, the webhook receiver, and range/time filtering are owned HERE; RAG is consumed via the versioned `/v1` contract with additive-field tolerance (never hard-code today's response shape).
- **Adjacency-list + recursive-CTE graph is mandatory** (frozen `pgvector/pgvector:pg16` image; no Apache AGE/ltree; `cxa1_user` cannot `CREATE EXTENSION`). Keep the `GraphRetriever` seam so a later AGE/Neo4j swap touches no SharedCore.
- **Guardrails fail-closed**; `decision=block` → 422 `GUARDRAIL_VIOLATION`. Memory is best-effort (never fails an answer).
- **`mcp-eng-memory` is STATELESS** — no DB/Kafka/outbox. Per-invocation metering is the calling xAgent's outbox, never the tool's. The product meters its OWN usage on its own topic.
- **`outbox` has NO RLS** by design (cross-tenant publish queue; isolation in the payload).
- **`auth.service_acl` seed uses the canonical columns** `(caller_service, target_service, allowed_scopes)` — NOT the rag-seed's buggy `(source_service, scopes)`.
- **Accept-but-ignore reserved JWT claims** (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) for Phase-13 hardening; never gate logic on their absence.

## Gotchas & current status
- **MVP drives ingestion/extraction via the authenticated API** (`/v1/connectors/github/sync`, `/v1/extract`); the Kafka worker (`worker/runner.py`) is a documented scale-out seam, not yet a live consumer.
- **Webhook path is graph-only** (`/webhooks/{kind}?tenant=<uuid>`): no inbound agent JWT to forward to RAG, so embedding is deferred to an authenticated sync/worker. Signature-verified.
- **Keyless GitHub fixtures** include explicit `owns`/`depends_on` edges so `who_owns`/`what_breaks` work without an LLM; extraction enriches when a real provider is configured.
- **Windows dev:** `main.py`/`__main__.py` force `WindowsSelectorEventLoopPolicy` (psycopg3 async). No-op on Linux/macOS.
- **Cross-team coordination:** `cypherx_a1` was added to `infra/dev/local/seed/postgres-init.sql` and `infra/modules/postgres-bootstrap/main.tf` (both closed enumerations). The cloud bootstrap needs Doppler `db/cypherx-a1/{runtime,ddl}_password`.
- The `mcp-eng-memory` facade is Valkey-free by design (revocation is enforced at the cypherx-a1 backend it forwards to).
