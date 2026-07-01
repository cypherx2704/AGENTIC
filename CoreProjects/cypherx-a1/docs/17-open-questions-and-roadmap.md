# 17 — Open questions & roadmap

> The decisions that are **locked** for the cypherx-a1 MVP, the handful that remain **open** (and the seam that absorbs each one), and the **Phase-2+ roadmap** — every future change designed to land behind an existing seam so it touches neither SharedCore nor the wire contracts.

This is the forward-looking companion to the rest of the `docs/` set. It does not re-derive the architecture (see `01`–`16`); it records *what is settled, what is not, and what comes next*. Every "open item" below names the **concrete code seam** that will absorb its eventual resolution, so the design never has to move when the answer arrives.

---

## 1. Resolved-decisions recap

These are **locked** and load-bearing. They are repeated here as a single index so the roadmap reads against a fixed baseline; the authoritative statements live in the platform root `../../CLAUDE.md`, this repo's `CLAUDE.md`, and the ADRs.

| # | Decision | Where it lives in code | Why it is locked |
|---|----------|------------------------|------------------|
| D1 | **Consuming app, not a SharedCore service.** Peer of `xAgent/ax-1`. Owns all domain logic; pushes none into SharedCore. | Whole repo; `services/` holds only thin `/v1` clients. | Keeps SharedCore generic; lets cypherx-a1 ship on its own cadence. |
| D2 | **One tenant per org, shared graph, app-owned ACLs.** | `cypherx_a1.resource_acls` table; RLS by `tenant_id` (role `cxa1_user`, `FORCE ROW LEVEL SECURITY`). | Per-repo/team visibility is product logic, not a SharedCore concern. |
| D3 | **Copilot calls llms-gateway + guardrails directly.** Seam to xAgent deferred. | `copilot/service.py`; `services/llms_client.py`, `services/guardrails_client.py`. | The xAgent-delegation seam (see §3.1) is additive; direct is the MVP-correct path. |
| D4 | **GitHub-first connectors.** | `connectors/github/`, `connectors/registry.py`; `connector_mode` (`mock`\|`live`). | Proves the connector SPI with one real source before fanning out. |
| D5 | **The graph is the crown jewel and is APP-OWNED.** It never enters RAG, never enters Memory. | `db/graph_repo.py` (adjacency list + recursive CTE); `ingestion/pipeline.py` sends only opaque docs to RAG. | Cross-principal leakage + embedding cost if it leaked into RAG/Memory. |
| D6 | **RAG vectors only, with an explicitly PINNED embedding model.** Never the repointable `embed` alias. | `rag_embedding_model="text-embedding-3-small"`, `rag_embedding_dim=1536`; resolved model+dim persisted immutably in `cypherx_a1.rag_kbs` by `KbResolver` (`ingestion/pipeline.py`). | A drifting embedding space silently corrupts cross-KB similarity. |
| D7 | **Hybrid retrieval is app-side.** Keyword (tsvector), RRF fusion, rerank, query-expansion, the webhook receiver, and range/time filtering are owned HERE. RAG ships dense-only first cycle. | `retrieval/orchestrator.py` (`RetrievalOrchestrator.retrieve`, `retrieval_rrf_k=60`); `graph_repo.keyword_search`. | Consume RAG via `/v1` with additive-field tolerance; never hard-code today's response shape. |
| D8 | **Memory is copilot working memory only** (per-principal episodic). | `services/memory_client.py`; `copilot_memory_type="episodic"`, best-effort (never fails an answer). | The knowledge graph must not go here. |
| D9 | **Guardrails fail-closed.** `decision=block` → `422 GUARDRAIL_VIOLATION`. | `copilot/service.py`; `services/guardrails_client.py` (`/v1/check/input`, `/v1/check/output` with `input_text`). | Safety beats availability on the copilot path. |
| D10 | **Identity in headers only** (Contract 12): service token in `Authorization`, agent JWT in `X-Forwarded-Agent-JWT`, W3C trace propagated. Bodies carry **no** identity. | `services/service_token.py`; all `services/*` clients. | Anti-spoof; `tenant_id`/`agent_id` come from the verified JWT, request models are `extra="forbid"`. |
| D11 | **`llm_call_id` is the billing key; never rewrite the gateway's cost.** | `extraction/extractor.py` records `completion.llm_call_id` + `completion.usage.cost_usd` into `extraction_jobs`; Contract-19 usage on `cypherx.cypherxa1.usage.recorded`. | One authoritative cost source (the gateway). |
| D12 | **`mcp-eng-memory` is STATELESS** — no DB/Kafka/outbox; metering is the calling xAgent's outbox, never the tool's. | `mcp-eng-memory/` (separate package, no DB deps). | A tool server must not own state or billing. |
| D13 | **Adjacency-list + recursive-CTE graph is mandatory** behind a `GraphRetriever` seam (frozen `pgvector/pgvector:pg16`; no Apache AGE/ltree; `cxa1_user` cannot `CREATE EXTENSION`). | `db/graph_repo.py`. | Lets a later AGE/Neo4j swap touch no SharedCore. |
| D14 | **Accept-but-ignore reserved JWT claims** (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`). | `core/auth.py`. | Forward-compat for Phase-13 hardening; never gate logic on their absence. |
| D15 | **`outbox` has NO RLS**; isolation lives in the payload (`partition_key=tenant_id`). | `db/outbox.py`; migration `*__init.sql`. | Cross-tenant publish queue by design. |

> If any item below appears to reopen one of D1–D15, it does not — each open item is a **parameter** or an **additive seam**, never a reversal.

---

## 2. Remaining open items

Four substantive questions remain genuinely open for the MVP. Each is **bounded** — the architecture already absorbs every plausible answer — and each names its seam so resolving it is a config/data change, not a redesign.

### 2.1 Embedding model to pin (the production choice)

**Status:** the *mechanism* is locked (D6); the *value* is a placeholder default that production must deliberately ratify.

The MVP pins `rag_embedding_model = "text-embedding-3-small"` at `rag_embedding_dim = 1536` (`core/config.py`). Every KB is created with this **explicit** name via `KbResolver.resolve()` in `ingestion/pipeline.py`, and the *resolved* model + dim are written **immutably** into `cypherx_a1.rag_kbs` at first use. That immutability is the whole point: once a KB exists, its vector space is frozen.

**Open question:** *which* model to ratify for the first production tenant, given the pin can never be silently changed afterward.

| Constraint | Detail |
|------------|--------|
| Dimension | `1536` is the only platform-supported dimension today (`rag_embedding_dim`). A different-dim model is a platform-level change, not a config tweak. |
| No alias | Must be a concrete model name the llms-gateway resolves to a stable embedding family — **never** the repointable `embed` alias (that alias drifting would corrupt cross-KB similarity). |
| Cross-KB stability | All four logical KBs — `eng-code`, `eng-conversations`, `eng-docs`, `eng-incidents` — must share one space so RRF fusion across them is meaningful. `KbResolver` already enforces "one model per KB"; the open item is choosing the one model. |

**Seam that absorbs the answer:** `Settings.rag_embedding_model` + `Settings.rag_embedding_dim`. Changing the production pin is a Doppler/config change applied **before any KB is created** for that tenant.

**Migration risk if changed late:** because `rag_kbs.model` is immutable, switching models on an existing tenant means **re-embedding** every doc into a *new* KB id and re-pointing `vector_ref` on the affected entities — a backfill, not an in-place edit. The roadmap item §3.3 (re-ingest/backfill path) is the right home for that operation. **Decision deadline:** before the first production tenant's first authenticated sync.

### 2.2 Conflict-policy tuning (extraction supersession + confidence)

**Status:** the *bitemporal supersede mechanism* is locked; the *thresholds and tie-breaks* are unset.

The extractor (`extraction/extractor.py`) emits a constrained edge vocabulary — `_EXTRACTABLE_RELS = {"depends_on", "decided_in", "caused", "resolved", "expert_in", "mentions"}` — each with a `confidence` in `[0,1]`. On every pass it calls `graph_repo.supersede_extracted_edges(src_entity_id, extractor_version)` first, so bumping `extractor_version` (`"1.0.0"`) bitemporally retires the prior version's edges rather than duplicating. Idempotency keys on `(tenant_id, node_id, content_sha, extractor_version)` in `extraction_jobs` mean re-ingest never re-spends.

**Open questions — the *policy* layered on top of that mechanism:**

| Open knob | Current behaviour (MVP) | What production must decide |
|-----------|-------------------------|------------------------------|
| Confidence floor | `_parse_edges` clamps to `[0,1]`, defaults missing to `0.5`, and accepts **all** parsed edges. | A minimum-confidence cutoff below which an extracted edge is dropped (or retained-but-flagged). |
| Deterministic-vs-extracted conflict | Deterministic ingest edges (`owns`/`authored`/`reviewed`/`part_of`) and extracted edges (`depends_on` etc.) live in disjoint relation sets, so they don't currently collide. | Policy if/when an extracted relation *contradicts* a deterministic one (e.g. extracted `owns` vs ingested `owns`) — today extraction simply cannot emit the deterministic set, which sidesteps it. Revisit if the extractable vocabulary widens. |
| Multi-source agreement | A later pass supersedes the earlier one wholesale per `(src, extractor_version)`. | Whether to *merge* corroborating evidence across passes / sources and boost confidence, vs. last-writer-wins. |
| Stale-edge decay | Superseded only on a version bump or re-extraction of the same node. | Time-based decay for `valid_to IS NULL` edges whose source artifact has gone silent. |

**Seam that absorbs the answer:** `extraction/extractor.py` (`_parse_edges` for the floor; `_write` / `graph_repo.supersede_extracted_edges` for the conflict resolution) plus the `confidence` and `extractor_version` columns on `edges`. None of this touches the wire contracts — it is pure app-side graph policy. Tuning is expected once a real provider (not the mock) is generating edges at volume.

### 2.3 Connector credential storage (sealed-secret hardening)

**Status:** the *schema and boundary* are locked; the *sealing backend* is a stub-grade default.

GitHub-first connector config lives in `cypherx_a1.connectors`; per-connector credentials live in `cypherx_a1.connector_secrets` (**sealed**), with incremental progress tracked in `sync_cursors`. The MVP keyless path (`connector_mode="mock"`) replays bundled fixtures and needs no real secret; the live path reads `github_token` / `github_webhook_secret` from env (`core/config.py`).

**Open questions:**

| Open item | MVP state | Production target |
|-----------|-----------|-------------------|
| At-rest sealing | `connector_secrets` is the **sealed** store by design, but the MVP supplies the live token via env (`GITHUB_TOKEN`) rather than a per-tenant sealed row. | Per-tenant credentials sealed in `connector_secrets`, decrypted only in-process at sync time — never an env var per connector. |
| Sealing backend | Envelope-encryption approach to match the platform (the way `Shared Core/auth` envelope-encrypts signing keys). | A KMS/AES envelope (platform pattern) vs. a Doppler-only model; decide the key custodian and rotation story. |
| Webhook secret scope | One `github_webhook_secret` from env, verified in `api/webhooks.py`. | Per-tenant / per-connector webhook secrets stored sealed, so one org's secret rotation can't disrupt another's. |
| Rotation | Manual (rotate the env value, redeploy). | In-place rotation writing a new sealed row + cursor continuity, no re-backfill. |

**Seam that absorbs the answer:** `connectors/base.py` (the connector SPI already takes credentials by injection) + the `connector_secrets` table + a sealing helper in `services/`. The webhook path stays signature-verified (`api/webhooks.py`) regardless of where the secret is stored. Because the connector SPI takes credentials by injection, swapping "env-var token" for "sealed-row token" is invisible to `connectors/github/`.

### 2.4 Copilot billing granularity

**Status:** the *cost source* is locked (D11 — the gateway's `llm_call_id`/`cost_usd`, never rewritten); the *roll-up granularity the product reports* is open.

Two cost-bearing copilot/extraction calls are already attributed and metered:

- **Extraction** records `completion.llm_call_id` + `completion.usage.cost_usd` into `extraction_jobs` (`extraction/extractor.py`) and the app emits Contract-19 usage on `cypherx.cypherxa1.usage.recorded` (`usage_topic` in `core/config.py`).
- **Copilot answers** go through `services/llms_client.py` the same way (`copilot_model="smart"`).

**Open questions — *how finely* the product attributes its own usage, on top of the gateway's authoritative per-call cost:**

| Granularity axis | MVP | Open choice for production |
|------------------|-----|-----------------------------|
| Per-call vs per-question | One copilot answer = potentially several gateway calls (guardrails-in screening is gateway-free, but a rerank/expansion future leg could add calls). | Roll up to a single "question" unit, or report each `llm_call_id` separately. |
| Embeddings attribution | Embeddings are reached **indirectly via RAG** during ingest, not on the copilot read path. | Whether ingest-time embedding cost is attributed to the *connector sync* that triggered it vs. amortized per tenant. |
| Tenant vs agent vs repo | Usage event carries `tenant_id` (partition key) + `request_id`. | Whether to break usage down by `agent_id` and/or by the repo/team ACL scope that the question touched. |
| Free vs metered reads | Graph-only reads (`/v1/graph/*`) spend no LLM tokens. | Whether to surface a non-LLM "query unit" so graph traffic is visible in billing even though it costs no gateway tokens. |

**Hard invariant (not open):** cypherx-a1 **never rewrites** the gateway's `cost_usd`. Any roll-up the product reports is a *sum/group-by* over the gateway's authoritative numbers keyed by `llm_call_id`, emitted on the app's own `cypherx.cypherxa1.usage.recorded` topic. Granularity is a reporting decision, not a costing one.

**Seam that absorbs the answer:** the Contract-19 usage event payload + `usage_topic`; the `extraction_jobs` cost ledger already proves the pattern for a parallel copilot ledger if per-question roll-up is chosen.

---

## 3. Phase-2+ roadmap

Every roadmap item is designed to land **behind an existing seam**. The guiding rule: *a future change must touch neither SharedCore nor the published `/v1`/MCP contracts.* The seams below already exist in code precisely so these can be additive.

### 3.1 xAgent-delegation seam (copilot via xAgent)

**Today:** the copilot calls llms-gateway + guardrails **directly** (D3) — `copilot/service.py` orchestrates PRE_GUARDRAIL → LLM → POST_GUARDRAIL itself. This is the MVP-correct path and is fully testable keyless.

**Phase-2:** route the copilot's reasoning turn through `xAgent/ax-1` instead of calling the gateway directly, so cypherx-a1 becomes an A2A *caller* of a general agent runtime and inherits its stage pipeline, behavior policies, and (eventually) tool loop. The reserved JWT claims already accepted-and-ignored (`delegation_*`, `behavior_policy_id`, `approval_context`, `wkl_id`, `cnf` — `core/auth.py`, D14) are exactly the Phase-13 hardening surface this seam will start honouring.

**Seam:** the copilot service boundary. The retrieval orchestrator (`retrieval/orchestrator.py`) and citation model (`models/api.Citation`) stay unchanged — cypherx-a1 keeps owning hybrid retrieval and citations; only the *answer-generation* hop moves from "llms-gateway directly" to "xAgent A2A (Contract 3)". Guardrails screening can move with it (xAgent owns its own pre/post-guardrail stages) or stay app-side; both are compatible. **No SharedCore change, no contract change.**

### 3.2 Async worker wiring (the scale-out path)

**Today:** ingestion + extraction run **synchronously** through the authenticated API — `/v1/connectors/{kind}/sync` and `/v1/extract` — which is fully testable keyless. `worker/runner.py` is a documented seam: `run_worker()` currently configures logging, logs `worker_started` (note: *"Kafka consumer wired in Phase 1.5"*), and idles on a 30-second `worker_heartbeat` loop so the worker process is a **no-op rather than a crash** when `CYPHERXA1_RUN_WORKER=1` selects it.

**Phase-1.5:** wire the Redpanda consumer group (`ingestion_consumer_group="cypherx-cypherxa1-workers"`) over the work topics under `ingestion_topic_prefix="cypherx.cypherxa1"` — the documented flow `raw.landed → record.normalized → extraction.requested → extraction.completed`, mirroring the rag-service ingestion worker split. Critically, the worker **re-uses the SAME functions** as the API path:

| Sync API today | Async worker (Phase 1.5) | Shared function |
|----------------|--------------------------|------------------|
| `POST /v1/connectors/{kind}/sync` | consume `…raw.landed` / `…record.normalized` | `ingestion.pipeline.ingest_records` (`_ingest_one`) |
| `POST /v1/extract` | consume `…extraction.requested` | `extraction.extractor.run_extraction` |

Because both paths funnel through `ingest_records` and `run_extraction`, wiring the worker adds **no new business logic** — it adds a transport (a consumer-group poll loop) and a **service-minted principal** so the worker has identity without an inbound agent JWT. This also unblocks the webhook path's deferred embedding (below).

**Seam:** `worker/runner.py` (`run_worker`); `worker_enabled`, `worker_max_attempts=3`, and the topic/group settings already exist. The paired `.dlq` topics (Contract-5, `partition_key=tenant_id`) are reserved for the consumer's retry-exhaustion path.

**Closes a known MVP gap:** the webhook path (`/webhooks/{kind}?tenant=<uuid>`) is **graph-only** today because it has no inbound agent JWT to forward to RAG — `ingest_records` is called with `rag=None`/`agent_jwt=None`, so `_ingest_one` lands + normalizes the graph and **defers** embedding. The service-minted-principal worker is the authenticated path that retroactively embeds those deferred docs.

### 3.3 Real graph-DB / vector-backend swaps (behind the seams)

**Graph backend.** Adjacency-list + recursive-CTE is **mandatory** today (D13) on the frozen `pgvector/pgvector:pg16` image — no Apache AGE, no ltree, and `cxa1_user` cannot `CREATE EXTENSION`. The entire graph surface is funnelled through `db/graph_repo.py` (`find_entities`, `keyword_search`, `upsert_entity`, `upsert_edge`, `supersede_extracted_edges`, `set_vector_ref`, recursive-CTE reads). That funnel **is** the `GraphRetriever` seam.

**Phase-2+:** swap the backend (Apache AGE in-Postgres once the image/extension policy allows, or an external Neo4j) by re-implementing `graph_repo` against it — **touching no SharedCore and no `/v1` contract**, because the graph is entirely app-owned and never crosses a service boundary. The recursive traversals used by `who_owns` / `what_breaks` (`copilot/queries.py`) become native graph queries; the bitemporal columns (`valid_from`/`valid_to`, partial unique index on the current slice) map to the target's temporal model.

**Vector backend.** cypherx-a1 never stores vectors itself — RAG owns the dense corpus, reached only via `RagClient` (`retrieval/orchestrator.py` leg 2, `RagClient.query`; ingest via `ingest_inline`). Swapping RAG's vector backend is a SharedCore concern that cypherx-a1 is insulated from by construction: it consumes RAG via `/v1` with **additive-field tolerance** (it reads `chunk_id`/`doc_id`/`content`/`score`/`source_name`/`source_uri`/`metadata` and ignores unknown fields). The one app-side coupling is the **pinned embedding model** (§2.1) — a *RAG* backend swap that changes the embedding space is the §2.1 re-embed/backfill operation, not a `graph_repo` change.

| Swap | Seam | Blast radius |
|------|------|--------------|
| Postgres adjacency → Apache AGE / Neo4j | `db/graph_repo.py` (the `GraphRetriever` seam) | App-internal only; no SharedCore, no `/v1`, no MCP change. |
| RAG vector engine (HNSW params, store) | `services/rag_client.py` via `/v1` additive tolerance | None app-side, unless embedding space changes → §2.1 backfill. |
| Keyword leg (tsvector → external BM25) | `graph_repo.keyword_search` + RRF in `orchestrator.py` | App-internal; RRF fusion contract unchanged. |

### 3.4 Phase-13 hardening readiness

cypherx-a1 is built to *slot into* Phase-13 hardening without rework. The pre-positioned hooks:

| Hardening surface | Pre-positioned today | Phase-13 action |
|-------------------|----------------------|-----------------|
| Reserved JWT claims | `core/auth.py` **accepts-but-ignores** `cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context` (D14) — never gates on their absence. | Begin *honouring* them (proof-of-possession `cnf`, delegation chains, behavior policy) as Auth lights them up. |
| Live revocation | Verifier-side **mirror** of the shared Valkey kill-switch (`revocation_key_prefix="cypherx:rev:"`, `revocation_check_enabled`, 150 ms timeout, **fails open** for availability). | Tighten the fail-open posture / add hard-fail mode where SLAs allow. |
| Guardrails | Fail-closed, `decision=block` → `422 GUARDRAIL_VIOLATION` (D9). | Add output-class policy granularity as guardrails contracts expand. |
| Tenant isolation | RLS + `FORCE ROW LEVEL SECURITY`, `cxa1_user` has no `BYPASSRLS`, NULLIF guard on `app.tenant_id`; `outbox` deliberately NO-RLS (D15). | Audit-log + cross-tenant assertion tests as part of the hardening gate. |
| Contract-15 cases 1–10 | Must not break the spine smoke tests. | Keep green; cypherx-a1 consumes only `cypherx.tenant.*` events and never regresses the spine. |
| Service-to-service ACL | `auth.service_acl` seeded from this repo's own `0002__seed.sql` with the **canonical** columns `(caller_service, target_service, allowed_scopes)` — not the rag-seed's buggy `(source_service, scopes)`. | Scope-tighten `allowed_scopes` as least-privilege review lands. |

> Readiness is *passive*: nothing in §3.4 requires a redesign — each row is a switch to flip or a policy to narrow once the corresponding platform capability ships.

### 3.5 External-IDE MCP bridge (`mcp-eng-memory` reach)

**Today:** `mcp-eng-memory` is a **stateless** MCP facade (`mcp-eng-memory/`, separate package, `manifest.json`, host **8094**) that proxies cypherx-a1's query API over the MCP contract (Contract 4, validated against `contracts/mcp/manifest.schema.json`). It registers as `mcp-eng-memory@1.0.0` in the Tool Registry and is invoked by xAgent via `POST /mcp/v1/invoke`. It holds **no** DB/Kafka/outbox; revocation is enforced at the cypherx-a1 backend it forwards to; per-invocation metering is the **calling xAgent's** outbox, never the tool's (D12).

**Phase-2+:** extend the same stateless facade so **external AI coding agents in a developer's IDE** (Claude Code, Cursor, Copilot-style agents) can query engineering memory directly — "who owns this / what breaks if I change X / why was this decided" — over the standard MCP transport.

| Roadmap step | Detail | Invariant preserved |
|--------------|--------|---------------------|
| IDE-facing MCP transport | Expose `mcp-eng-memory` over the stdio / streamable-HTTP transport an external IDE agent speaks, alongside the in-platform `/mcp/v1/invoke` path. | Still **stateless**; still forwards to the cypherx-a1 backend for all logic + RLS. |
| Identity for external callers | An external IDE agent presents an agent JWT (or an exchanged credential); the facade forwards it as `X-Forwarded-Agent-JWT` to cypherx-a1, which re-verifies against Auth JWKS and applies tenant RLS + resource ACLs. | Identity in **headers only**; `tenant_id`/`agent_id` from the verified JWT, never a body (D10). |
| Metering for external callers | When invoked *outside* an xAgent, the **product** meters its own usage on `cypherx.cypherxa1.usage.recorded` (it already does for its own LLM spend); the tool itself still emits nothing. | Metering is the caller's outbox in-platform; the product's own outbox out-of-platform — the facade never grows state (D12). |
| Tool versioning | Ship as `mcp-eng-memory@1.1.0+` with the manifest re-validated against `contracts/mcp/manifest.schema.json`; add tools (e.g. richer `what_breaks`) additively. | Contract-4 manifest stays valid; existing tools unchanged. |

**Seam:** the `mcp-eng-memory/` package boundary + its `manifest.json`. Because every read already lands on cypherx-a1's authenticated query API (which owns RLS, ACLs, and citations), broadening *who* can call the facade adds reach without moving a single piece of domain logic out of the product service.

---

## 4. Roadmap-at-a-glance

| Item | Phase | Type | Seam | Touches SharedCore / contracts? |
|------|-------|------|------|----------------------------------|
| Pin production embedding model (§2.1) | now → pre-prod | Decision (config) | `Settings.rag_embedding_model/dim`, `rag_kbs` | No |
| Conflict-policy tuning (§2.2) | now → as volume grows | App-side policy | `extraction/extractor.py`, `edges.confidence/extractor_version` | No |
| Connector credential sealing (§2.3) | Phase-2 | Hardening | `connectors/base.py`, `connector_secrets` | No |
| Copilot billing granularity (§2.4) | Phase-2 | Reporting | `usage_topic`, Contract-19 payload | Contract-19 (additive only) |
| xAgent-delegation seam (§3.1) | Phase-2 | Additive seam | copilot service boundary | A2A Contract 3 (caller), no SharedCore change |
| Async worker wiring (§3.2) | Phase-1.5 | Transport | `worker/runner.py` | No (re-uses existing functions) |
| Graph-DB swap (§3.3) | Phase-2+ | Backend swap | `db/graph_repo.py` (`GraphRetriever`) | No |
| Vector-backend swap (§3.3) | Phase-2+ | SharedCore-side | `services/rag_client.py` `/v1` tolerance | No app-side (unless re-embed) |
| Phase-13 hardening (§3.4) | Phase-13 | Passive readiness | `core/auth.py`, revocation mirror, RLS | No (flip switches as Auth lights up) |
| External-IDE MCP bridge (§3.5) | Phase-2+ | Reach | `mcp-eng-memory/` + `manifest.json` | Contract-4 manifest (additive), no logic moved |

> **The through-line:** nothing on this roadmap requires moving domain logic into SharedCore, breaking a `/v1` or MCP contract, or regressing Contract-15 cases 1–10. Every future change is a value to pin, a policy to tune, a transport to wire, or a backend to re-implement behind a seam that already exists in the code.
