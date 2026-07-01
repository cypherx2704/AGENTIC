# Build plan & phasing

> **cypherx-a1 is built in dependency order, contract-first.** Phase 0 (foundations) and Phase 1 (GitHub-first MVP slice — ingest → graph + RAG → cited copilot) and Phase 2 (stateless `mcp-eng-memory@1.0.0`) are **DONE**; Phase 3 (hardening & scale — Jira/Slack connectors, ACL enforcement, transitive-closure precompute, conflict policy, usage metering, runbooks) is the forward roadmap. Every phase honours the SharedCore `/v1` contracts, never the reverse, and none of them may break Contract-15 smoke cases 1–10.

---

## 1. Phasing philosophy

cypherx-a1 follows the same discipline as the wider platform (see `../../CLAUDE.md` §"Build phases"): **build in dependency-ordered phases, each one honouring the immutable cross-service contracts.** The product never pushes business logic into SharedCore; it adapts to the published `/v1` shapes. Three rules govern the order:

1. **Foundations before features.** Schema, RLS, auth, error/trace/log/health plumbing, and the service-token seam must exist and be testable before any domain code lands — otherwise every later phase re-litigates the same boundary.
2. **One connector proven end-to-end before breadth.** A single source (GitHub) driven the *whole way* through ingest → graph → RAG → extraction → retrieval → cited answer → MCP is worth more than six half-wired connectors. The canonical model (`models/canonical.py`) is connector-agnostic so later sources are **additive, not rewrites** (see `05-ingestion-and-connector-spi.md`).
3. **The leverage surface (MCP) ships before scale.** Other agents are the big 2026 market (`00-overview-and-product-vision.md` §2.7), so the stateless `mcp-eng-memory` facade is a Phase-2 deliverable, ahead of the operational hardening that turns the MVP into a fleet-ready service.

Each phase has an explicit **Definition of Done (DoD)** and an explicit set of **forward-compatibility guarantees** it must preserve for the next phase. The DoD is what the phase *delivers*; the guarantees are what it *promises not to break*.

| Phase | Name | Scope | Status |
|-------|------|-------|--------|
| 0 | Foundations | Schema + RLS + roles, auth/JWKS, Contract 2/5/6/7/8/12/13/14 plumbing, service-token seam, SharedCore clients, app skeleton | **DONE** |
| 1 | MVP — GitHub-first slice | GitHub connector (mock + live), normalizer + identity resolution, graph upsert, RAG ingest with pinned model, LLM extraction, hybrid RRF retrieval, cited copilot, graph-query API | **DONE** |
| 2 | MCP server | Stateless `mcp-eng-memory@1.0.0` Contract-4 facade over the query API; Tool-Registry registration | **DONE** |
| 3 | Hardening & scale | Jira + Slack connectors, `resource_acls` enforcement, transitive-closure precompute, edge-conflict policy, Contract-19 usage metering hardening, Kafka worker activation, runbooks | **roadmap** |

---

## 2. Phase 0 — Foundations (DONE)

**Goal:** a deployable, observable, tenant-isolated, authenticated empty service that owns the `cypherx_a1` schema and can mint service tokens — *before any product logic exists*.

### 2.1 What it delivered

| Area | Deliverable | Where |
|------|-------------|-------|
| Schema + RLS | `cypherx_a1` schema, all tables, `FORCE ROW LEVEL SECURITY`, `app.tenant_id` `SET LOCAL` policy with `NULLIF` guard, runtime role `cxa1_user` (LOGIN, **no** BYPASSRLS, **no** CREATE EXTENSION) | `db/migrations/20260614_0001__init.sql` |
| Service-ACL seed | `auth.service_acl` edges so Auth lets `cypherx-a1` mint a service token, using the **canonical** columns `(caller_service, target_service, allowed_scopes)` | `db/migrations/20260614_0002__seed.sql` |
| Inbound auth | JWKS RS256 verify, `iss`/`aud`/`exp`/`sub` checks (±60 s skew), scope gating, revocation MIRROR (fail-open) | `src/cypherx_a1/core/auth.py` |
| Config | pydantic-settings (no prefix, Doppler-compatible); `SERVICE_BOOTSTRAP_SECRET` **required, no default** (fails fast at boot) | `src/cypherx_a1/core/config.py` |
| Contract plumbing | Contract 2 errors, Contract 6 structlog JSON, Contract 7 health, Contract 8 W3C trace + OTel opt-in, Contract 5 outbox envelope | `src/cypherx_a1/core/{errors,logging,trace,metrics}.py`, `src/cypherx_a1/db/outbox.py` |
| Service-token seam | Contract-12 token mint + `X-Forwarded-Agent-JWT` forwarding scaffolding | `src/cypherx_a1/services/service_token.py` |
| App skeleton | App factory + lifespan (DB pool, clients, JWKS warm), health router | `src/cypherx_a1/main.py`, `src/cypherx_a1/api/health.py` |

### 2.2 Identity & tenancy foundation

The locked identity posture is established here and is unchanged by every later phase:

- **Inbound:** callers present a bare agent JWT in `Authorization`. `require_principal` (`core/auth.py`) re-verifies it locally against the Auth JWKS and resolves a `Principal`. `tenant_id` and `agent_id` come **only** from the verified token (Contract 13) — never a request body. The verified bearer is preserved on `Principal.raw_token`.
- **Outbound:** every SharedCore call carries the Contract-12 **service token** in `Authorization` plus `X-Forwarded-Agent-JWT: <Principal.raw_token>` plus the W3C `traceparent`. **Bodies carry no identity.**
- **Scopes:** `SCOPE_QUERY = "cypherxa1:query"`, `SCOPE_INGEST = "cypherxa1:ingest"`, `SCOPE_ADMIN = "cypherxa1:admin"`. Platform/admin scopes (`agent:execute`, `agent:admin`, `platform:admin`) are also admitted at the dependency so an admin JWT is not 403'd before an endpoint's finer check (`query_scopes()`, `ingest_scopes()`, `admin_scopes()`).

### 2.3 Definition of Done (Phase 0)

- [x] `docker compose --profile migrate up migrate` applies `*__init.sql` then `_0002__seed.sql` against Neon DIRECT, creating schema `cypherx_a1` + role `cxa1_user` + the `auth.service_acl` edges; idempotent, exits 0.
- [x] Service boots with `SERVICE_BOOTSTRAP_SECRET` set; **fails fast** if it is missing.
- [x] `/livez` returns 200 (process-only); `/readyz` returns 503 until Postgres + warm Auth JWKS are reachable, then 200; `/metrics` exposes Prometheus on the Contract-7 port.
- [x] `cypherx-a1` can mint a Contract-12 service token at Auth `POST /v1/service-tokens`.
- [x] RLS proven: a query under tenant A cannot see tenant B's rows (the `app.tenant_id` `SET LOCAL` + `FORCE RLS` path).
- [x] `uv run pytest` is network-free and green; `ruff` + `mypy` clean.
- [x] Cross-team bootstrap landed: `cypherx_a1` added to `infra/dev/local/seed/postgres-init.sql` and `infra/modules/postgres-bootstrap/main.tf` (both closed enumerations).

### 2.4 Forward-compat guarantees Phase 0 must preserve

- The `outbox` table has **NO RLS** by design (cross-tenant publish queue; isolation lives in the Contract-5 payload). Later phases must not "fix" this.
- The runtime role `cxa1_user` cannot `CREATE EXTENSION` — the graph stays on the frozen `pgvector/pgvector:pg16` image via adjacency-list + recursive CTE. No Apache AGE / ltree dependency may be introduced.
- Reserved JWT claims (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) are **accepted but ignored** — never gate logic on their absence (Phase-13 platform hardening will populate them).

---

## 3. Phase 1 — MVP GitHub-first slice (DONE)

**Goal:** prove the whole thesis end-to-end with one connector — *ingest GitHub → tenant-scoped knowledge graph + RAG corpus → LLM extraction → cited hybrid answer* — and expose it as a public, authenticated API.

### 3.1 The slice, stage by stage

```
GitHub (mock fixtures | live API)
  → connector (connectors/github.py)            CanonicalRecord[]
  → normalizer (ingestion/normalizer.py)        nodes + edges + identity resolution
  → pipeline (ingestion/pipeline.py)            landing → graph upsert → RAG ingest → citation
  → extractor (extraction/extractor.py)         LLM json_object pass, bitemporal supersede
  → orchestrator (retrieval/orchestrator.py)    graph + RAG-dense + tsvector → RRF fusion → cited
  → copilot (copilot/service.py)                guardrails-in → llms answer → guardrails-out → memory
```

### 3.2 Public API delivered

| Endpoint | Router | Scope | Purpose |
|----------|--------|-------|---------|
| `POST /v1/connectors/{kind}/sync` | `api/connectors.py` | ingest | Bounded backfill from a connector (resumable via `sync_cursors`) |
| `POST /v1/extract` | `api/connectors.py` | ingest | Run/replay the LLM extraction pass over landed content |
| `POST /webhooks/{kind}` | `api/webhooks.py` | (HMAC, no JWT) | Signature-verified inbound events; **graph-only** (embedding deferred) |
| `POST /v1/copilot/ask` | `api/copilot.py` | query | The cited copilot flow (guardrails-screened) |
| `POST /v1/graph/who-owns` | `api/graph.py` | query | Ownership lookup |
| `POST /v1/graph/what-breaks` | `api/graph.py` | query | Blast-radius / dependency lookup |
| `POST /v1/graph/experts` | `api/graph.py` | query | Expert ranking |
| `POST /v1/graph/why-built` | `api/graph.py` | query | Decision rationale |
| `POST /v1/graph/neighbors` | `api/graph.py` | query | Raw neighbour traversal |

> The graph routes are mounted under the `/v1/graph` prefix (router declares the leaf paths `/who-owns`, `/what-breaks`, `/experts`, `/why-built`, `/neighbors`).

### 3.3 RAG corpus + pinned embedding model

Phase 1 stands up the four per-tenant knowledge bases with an **explicit pinned embedding model** — never the repointable `embed` alias — so the whole corpus shares one stable vector space (`04-rag-kb-design.md`):

| KB (logical name) | Config key | Holds |
|-------------------|-----------|-------|
| `eng-code` | `rag_kb_code` | code / PR diffs / file content |
| `eng-conversations` | `rag_kb_conversations` | PR comments, review threads, chat |
| `eng-docs` | `rag_kb_docs` | READMEs, design docs |
| `eng-incidents` | `rag_kb_incidents` | incident / postmortem text |

Pin: `rag_embedding_model = "text-embedding-3-small"`, `rag_embedding_dim = 1536` (the only platform-supported dimension). The resolved `kb_id` + model + dim are persisted **immutably** in `cypherx_a1.rag_kbs` at first use. Retrieval clamps respect the RAG server caps: `rag_query_top_k = 20` (server caps 100), `rag_query_ef_search = 100` (server caps 500), inline ingest `<= 100 KiB`. **The graph never enters RAG** — `rag.chunks` are opaque text + metadata.

### 3.4 LLM extraction

`extraction/extractor.py` runs an idempotent, cost-metered LLM pass via llms-gateway (`response_format = json_object`):

- Model `extraction_model = "smart"`, `extraction_max_tokens = 1024`, `extraction_temperature = 0.0`.
- **Idempotency + cost ledger:** keyed on `extractor_version = "1.0.0"` in `extraction_jobs`; bumping the version **supersedes** prior extracted edges (bitemporal `valid_to`) without re-spending on unchanged content.
- `llm_call_id` is the billing key; the gateway's cost is **never rewritten** (Contract 19).

The keyless GitHub fixtures embed explicit `owns` / `depends_on` edges so `who_owns` / `what_breaks` work without an LLM at all; extraction *enriches* the graph when a real provider is configured.

### 3.5 Hybrid retrieval (RRF)

`retrieval/orchestrator.py` fuses three signals app-side (RAG ships dense-only first cycle):

| Signal | Source | Limit knob |
|--------|--------|-----------|
| Graph | recursive-CTE traversal over `entities`/`edges` | `retrieval_graph_limit = 20`, `retrieval_max_hops = 3` |
| RAG-dense | `POST /v1/kbs/{id}/query` | `rag_query_top_k = 20` |
| Keyword | `entities` `fts` tsvector generated column | `retrieval_keyword_limit = 20` |

Fusion is **Reciprocal-Rank Fusion** with `retrieval_rrf_k = 60` (the canonical default in `1/(k+rank)`). RAG hits are mapped back to their originating entity via the `doc_id` citation link so a chunk and its entity reinforce each other. Context is bounded to `retrieval_context_max_chunks = 12`. **Keyword, RRF fusion, rerank, query expansion, and the webhook receiver are all owned HERE** — never pushed into RAG.

### 3.6 Cited copilot

`copilot/service.py` (`copilot_model = "smart"`, `copilot_max_tokens = 1024`, `copilot_temperature = 0.2`):

1. **Pre-guardrail:** `POST /v1/check/input` (passing `input_text`). **Fail-closed** — `decision=block` → `422 GUARDRAIL_VIOLATION`.
2. **Answer:** llms-gateway with the RRF-fused, cited context. Every surviving evidence item is a `Citation` — the copilot can never return an uncited answer.
3. **Post-guardrail:** `POST /v1/check/output` (passing the answer as `input_text`). Same fail-closed posture.
4. **Memory (best-effort):** when `copilot_memory_enabled` (default true), the episodic turn is written to memory-service (`copilot_memory_type = "episodic"`). **Memory failure never fails an answer.** The knowledge graph **must not** enter Memory (per-principal; cross-principal leakage + embedding cost).

### 3.7 Connector mode

`connector_mode = "mock"` replays bundled GitHub fixtures for a fully keyless/offline local run; `connector_mode = "live"` calls `github_api_url = "https://api.github.com"` with `github_token`. Backfill is bounded per tick (`backfill_page_size = 100`) and resumable via `sync_cursors`. Webhooks are HMAC-verified with `github_webhook_secret`.

### 3.8 Definition of Done (Phase 1)

- [x] `POST /v1/connectors/github/sync` (mock mode) lands raw events idempotently into `raw_events`, normalizes them, upserts `entities` + `edges`, ingests text into the four RAG KBs, and writes `citations` — under tenant RLS.
- [x] `POST /v1/copilot/ask` returns a guardrails-screened, **cited** answer; an uncited answer is impossible by construction.
- [x] All five `/v1/graph/*` routes return correct results against the fixtures **without** an LLM (explicit fixture edges).
- [x] `POST /v1/extract` is idempotent on `(extractor_version, content)`; re-running does not re-spend; bumping `extractor_version` supersedes via bitemporal `valid_to`.
- [x] Every downstream call carries the Contract-12 service token + `X-Forwarded-Agent-JWT` + `traceparent`; no identity appears in any body.
- [x] RAG KBs are created with the pinned model (never `embed`); the binding is recorded in `rag_kbs`.
- [x] Outbox emits `cypherx.cypherxa1.record.normalized` via the Contract-5 envelope (`partition_key = tenant_id`).
- [x] `uv run pytest` green and network-free (respx-mocked SharedCore); `ruff` + `mypy` clean.

### 3.9 Forward-compat guarantees Phase 1 must preserve

- **Consume RAG strictly via `/v1` with additive-field tolerance** — never hard-code today's response shape; ignore unknown fields.
- **`@>`-containment filters only** when querying RAG — express time/range predicates as ISO strings and do the range filtering app-side.
- The `CanonicalRecord` model stays connector-agnostic so Jira/Slack are additive in Phase 3.
- The `GraphRetriever` seam stays intact so a later AGE/Neo4j swap touches no SharedCore and no caller.
- **Do not break Contract-15 smoke cases 1–10** (the platform spine: agent registered → JWT issued → task → guardrails in+out → LLM → response).

---

## 4. Phase 2 — MCP server (DONE)

**Goal:** expose the same query logic to AI coding agents as a Contract-4 MCP tool, so the memory is consumable by machines, not just a browser.

### 4.1 What it delivered

`mcp-eng-memory/` is a **separate, lean, stateless** package (`mcp_eng_memory`) with **no DB / Kafka / outbox / Valkey dependencies**. It is a thin Contract-4 facade that proxies cypherx-a1's authenticated query API:

| Property | Value |
|----------|-------|
| Tool name / version | `mcp-eng-memory@1.0.0` |
| Manifest | `mcp-eng-memory/manifest.json`, validated against `contracts/mcp/manifest.schema.json` (Contract 4) |
| Config | `CYPHERXA1_BASE_URL` (the backend it forwards to), `MANIFEST_PATH` |
| Ports | host **8094** → in-container **8080** |
| State | **none** — every query forwards to cypherx-a1, which enforces RLS, scopes, and revocation |

### 4.2 Metering ownership (load-bearing)

The MCP facade is stateless **by design** and emits **no usage** of its own. Per-invocation tool metering is the **calling xAgent's outbox**, never the tool's (platform rule — see `../../CLAUDE.md`). The cypherx-a1 product meters its **own** usage on its **own** topic (`cypherx.cypherxa1.usage.recorded`); the two are distinct and must not be conflated. The facade is also Valkey-free: revocation is enforced at the cypherx-a1 backend it forwards to.

### 4.3 Definition of Done (Phase 2)

- [x] `mcp-eng-memory/manifest.json` validates against `contracts/mcp/manifest.schema.json`.
- [x] The MCP server proxies the query tools (`who_owns`, `what_breaks`, `experts`, `why_built`, copilot ask) to cypherx-a1 with the forwarded identity headers intact.
- [x] The server holds no DB/Kafka/outbox/Valkey dependency and emits no usage event.
- [x] `mcp-eng-memory@1.0.0` is registerable in the Tool Registry.
- [x] Compose: `docker compose up -d --build cypherx-a1 mcp-eng-memory` brings both up (host 8093 / 8094).

### 4.4 Forward-compat guarantees Phase 2 must preserve

- The MCP facade stays **stateless** — no creeping DB/cache/outbox into it.
- Tool name/version is immutable; a breaking tool change is `mcp-eng-memory@2.0.0` alongside `@1.0.0`, never an in-place edit (Contract immutability).
- Metering ownership stays with the caller — the tool never emits per-invocation usage.

---

## 5. Phase 3 — Hardening & scale (roadmap)

**Goal:** turn the proven MVP into a multi-source, ACL-enforced, operationally observable fleet service. Phase 3 is breadth + hardening; it adds **no new SharedCore dependency** and changes **no published contract**.

### 5.1 Workstreams

| # | Workstream | What lands | Touches |
|---|-----------|-----------|---------|
| 3a | **Jira + Slack connectors** | Two new connectors behind the existing SPI; same `CanonicalRecord` → graph/RAG path; per-source webhook receivers | `connectors/`, `ingestion/` |
| 3b | **`resource_acls` enforcement** | Per-repo/team ACL checks applied to graph + retrieval reads (app-owned authz, on top of tenant RLS) | `resource_acls`, `retrieval/`, `copilot/`, `api/graph.py` |
| 3c | **Transitive-closure precompute** | Materialized reachability for hot `what_breaks` / dependency queries (recursive-CTE results cached) so deep traversals don't re-walk every request | `db/graph_repo.py`, new closure table |
| 3d | **Edge-conflict policy** | Deterministic resolution when extraction yields contradictory edges (bitemporal supersede + confidence/recency precedence) | `extraction/`, `ingestion/normalizer.py` |
| 3e | **Usage metering hardening** | Robust Contract-19 emission of `cypherx.cypherxa1.usage.recorded` (units + `request_id`); reconcile against gateway `llm_call_id` without rewriting cost | `db/outbox.py`, `services/llms_client.py` |
| 3f | **Kafka worker activation** | Promote `worker/runner.py` from documented seam to a live consumer (`ingestion_consumer_group = "cypherx-cypherxa1-workers"`) so ingestion/extraction run async at scale; `.dlq` + `worker_max_attempts = 3` retry | `worker/runner.py` |
| 3g | **Runbooks** | On-call runbooks: cold-start, RAG/guardrails/llms outage degradation, DLQ drain, re-extraction, backfill resume | `docs/` ops set |

### 5.2 Connector breadth (3a)

Phase 1 deliberately proved one connector end-to-end so the rest are additive. Jira and Slack land behind the same `connectors/base.py` SPI, producing the connector-agnostic `CanonicalRecord`; the normalizer, graph upsert, RAG ingest, extraction, and retrieval are **unchanged**. New webhook receivers reuse the HMAC-verified `POST /webhooks/{kind}` pattern (graph-only on the webhook path — embedding deferred to an authenticated sync/worker since the webhook carries no agent JWT to forward to RAG).

### 5.3 ACL enforcement (3b)

The locked tenancy model is *one tenant per org, shared graph within the tenant, plus app-owned per-repo/team ACLs* (`resource_acls`). Phase 1 owns the table; Phase 3 **enforces** it: graph and retrieval reads filter by the caller's repo/team grants on top of the always-on tenant RLS. This is **app-owned authz** — it is never pushed into Auth or any SharedCore service. Tenant RLS remains the hard isolation boundary; ACLs are an intra-tenant refinement.

### 5.4 Transitive-closure precompute (3c)

Deep `what_breaks` blast-radius queries currently walk the adjacency list with a recursive CTE on every request (bounded by `retrieval_max_hops = 3`). Phase 3 precomputes and caches reachability for hot subgraphs so traversal cost is amortized. This must stay **behind the `GraphRetriever` seam** and remain a pure optimization — results are identical to the live CTE; the closure is a cache, not a source of truth.

### 5.5 Edge-conflict policy (3d)

When repeated extraction passes (or multiple sources) produce contradictory edges (e.g. ownership reassigned), Phase 3 applies a deterministic policy: bitemporal supersede (close the old edge's `valid_to`, open the new) with **confidence + recency** precedence. The `extractor_version` bump path (which supersedes prior edges) already exists; conflict policy generalizes it to concurrent/cross-source contradictions.

### 5.6 Usage metering hardening (3e)

cypherx-a1 emits its **own** usage on `usage_topic = "cypherx.cypherxa1.usage.recorded"` (Contract 19): units + `request_id`, **never** a rewrite of the gateway's cost. Phase 3 hardens the emission (exactly-once-into-outbox semantics, reconciliation against the gateway `llm_call_id` billing key) and the `.dlq` path. The MCP facade still emits nothing — caller-owned metering is unchanged.

### 5.7 Worker activation (3f)

The MVP drives ingestion/extraction **synchronously** through the authenticated API (`/v1/connectors/github/sync`, `/v1/extract`). `worker/runner.py` already exists as a documented scale-out seam consuming `cypherx.cypherxa1.*` (`ingestion_topic_prefix`). Phase 3 makes it a live consumer with `worker_enabled` on, paired `.dlq` topics, and `worker_max_attempts = 3` — decoupling backfill throughput from request latency.

### 5.8 Definition of Done (Phase 3)

- [ ] Jira and Slack connectors land behind the existing SPI, produce `CanonicalRecord`, and flow through the unchanged graph/RAG/extraction path; their webhooks are HMAC-verified.
- [ ] `resource_acls` are **enforced** on graph + retrieval reads; a caller without a repo/team grant cannot see those entities even within the same tenant.
- [ ] Transitive-closure cache returns results identical to the live recursive CTE; stays behind the `GraphRetriever` seam.
- [ ] Edge-conflict policy resolves contradictory edges deterministically via bitemporal supersede + confidence/recency precedence.
- [ ] `cypherx.cypherxa1.usage.recorded` emission is hardened (units + `request_id`); gateway cost is never rewritten; `.dlq` drains.
- [ ] `worker/runner.py` runs as a live consumer with retry + DLQ; sync API path still works unchanged.
- [ ] Runbooks published for cold-start, SharedCore outage degradation, DLQ drain, re-extraction, and backfill resume.
- [ ] Contract-15 cases 1–10 still pass; no published contract changed.

### 5.9 Forward-compat guarantees Phase 3 must preserve

- **No new SharedCore dependency and no published-contract change.** Phase 3 is breadth + ops only.
- ACL enforcement is **app-owned** — never delegated into Auth/SharedCore.
- Closure precompute is a cache behind the seam — never a second source of truth, never an AGE/extension dependency.
- Worker activation must not change the synchronous API semantics; the API path remains a first-class entry, not a legacy fallback.
- Usage metering keeps the gateway cost authoritative (`llm_call_id`); the app never rewrites it.

---

## 6. Smoke-test alignment (Contract 15)

cypherx-a1 is a **consuming app**, not part of the platform spine, so it does not author new smoke cases for the spine. Its overriding obligation across **every** phase is negative: **do not break Contract-15 cases 1–10** (`contracts/smoke-tests/`), which gate the spine Phases 0–4 + 9A — *agent registered → JWT issued → task submitted → guardrails in+out → LLM called → response returned, observable end-to-end*.

The way cypherx-a1 stays aligned:

- It **reuses** the spine's auth (JWKS verify), llms-gateway, and guardrails exactly as xAgent does — same posture, same headers, same fail-closed guardrails — so exercising cypherx-a1 cannot regress the spine's contract behaviour.
- It consumes only `cypherx.tenant.*` platform events; it produces only its own `cypherx.cypherxa1.*` topics (+ `.dlq`), so it cannot pollute or starve a spine topic.
- Its own per-phase DoD checklists (§2.3, §3.8, §4.3, §5.8) are the product-level smoke gates; they are run in addition to — never instead of — the spine cases.

---

## 7. Forward-compatibility guarantees (cross-phase summary)

These hold for **all** phases and are the contract cypherx-a1 makes with the rest of the platform. They are the same set called out in `../../CLAUDE.md` "Phase alignment guarantees" and the repo CLAUDE.md invariants:

| Guarantee | Why |
|-----------|-----|
| Consume RAG strictly via `/v1` with **additive-field tolerance** | RAG ships dense-only first cycle; its response shape will grow — never hard-code today's. |
| Own keyword / RRF / rerank / query-expansion / webhook-receiver **app-side** | These are the hard, app-domain parts; pushing them into RAG would couple the corpus to product logic. |
| **Pin an explicit embedding model** (never the `embed` alias) | One stable vector space across all KBs; the alias can be repointed under us. |
| **`@>`-containment filters only** → ISO strings + app-side range filtering | The RAG query filter grammar is containment-only first cycle. |
| **Accept-but-ignore reserved JWT claims** (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) | Phase-13 platform hardening will populate them; never gate logic on their absence. |
| Consume only `cypherx.tenant.*` events | Stay inside the published event surface. |
| **Don't break Contract-15 cases 1–10** | The spine's definition of done outranks every product feature. |
| The **graph never enters RAG or Memory** | RLS-isolated crown jewel; RAG chunks are opaque, Memory is per-principal (cross-principal leak + embedding cost). |
| Keep the **`GraphRetriever` seam** (adjacency-list + recursive-CTE mandatory) | A later AGE/Neo4j swap must touch no SharedCore and no caller. |
| **`llm_call_id` is the billing key; never rewrite gateway cost** (Contract 19) | The gateway is the single source of LLM cost truth. |

---

## 8. Build / test / run per phase

The build/test/run loop is identical across phases (see repo `CLAUDE.md` for full detail):

```bash
uv sync
export SERVICE_BOOTSTRAP_SECRET=local-dev-cypherxa1-secret   # required, no default (fails fast)
uv run uvicorn cypherx_a1.main:app --reload --port 8093
uv run pytest                                                # network-free (respx-mocked SharedCore)
uv run ruff check src tests && uv run mypy
```

Compose (service `cypherx-a1`, host **8093**→8080; `mcp-eng-memory` host **8094**→8080):

```bash
docker compose --profile migrate up migrate                 # applies *__init.sql then _0002__seed.sql (Neon DIRECT)
docker compose up -d --build cypherx-a1 mcp-eng-memory       # deps → auth → llms+guardrails → rag+memory → cypherx-a1 → mcp
```

Keyless local: `CONNECTOR_MODE=mock` (bundled GitHub fixtures) + upstream `MOCK_PROVIDERS` / `MOCK_EMBEDDINGS`. Health (Contract 7): `/livez`, `/readyz`, `/metrics`.

---

## 9. References

- Product vision & the eight base-idea improvements: `00-overview-and-product-vision.md`
- SharedCore integration boundary: `02-sharedcore-integration-boundary.md`
- Data model & schema (bitemporal entities/edges, RLS): `03-data-model-and-schema.md`
- RAG KB design & pinned model: `04-rag-kb-design.md`
- Ingestion & connector SPI: `05-ingestion-and-connector-spi.md`
- Knowledge-extraction engine: `06-knowledge-extraction-engine.md`
- Hybrid retrieval & RRF: `07-hybrid-retrieval-and-reasoning.md`
- Copilot & public API: `08-copilot-and-public-api.md`
- Architecture decision records: `01-architecture-decision-records.md`
- Platform phases & contracts: `../../CLAUDE.md`, `../../archive/Manoj/phases/`
