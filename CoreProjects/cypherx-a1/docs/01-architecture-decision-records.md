# Architecture Decision Records

> The load-bearing, hard-to-reverse decisions behind **cypherx-a1** ("Autonomous Engineering Memory"): how the knowledge graph is stored and traversed, where vectors live, how the copilot reaches the model, how AI coding agents query the system, how tenancy and authorization are split, and how the graph stays correct over time. Each ADR records its **context**, the **decision**, the **alternatives rejected**, and the **consequences** — grounded in the real schema (`db/migrations/20260614_0001__init.sql`) and the shipped code under `src/cypherx_a1/`.

---

## What cypherx-a1 is (one paragraph of orientation)

cypherx-a1 ingests engineering sources (GitHub first), normalizes them into a **tenant-scoped knowledge graph** of `person / service / repo / feature / decision / incident / pr / ticket / document` nodes plus typed relationships, runs **LLM knowledge-extraction** over that graph, serves **hybrid retrieval** (graph + RAG-dense + keyword, fused with RRF), exposes a **cited AI copilot** (`POST /v1/copilot/ask`), and ships a **stateless MCP server** (`mcp-eng-memory`) so autonomous coding agents can query the same memory. It is a **consuming application** — a peer of `xAgent/ax-1`, not a SharedCore service. It reuses SharedCore (Auth, llms-gateway, RAG, Memory, Guardrails) strictly over versioned `/v1` contracts and pushes **no business logic into SharedCore**.

The decisions below are *locked*. They are recorded here so future work understands not just *what* was chosen but *why the alternatives were wrong for this app*, and what each choice costs.

### ADR index

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](#adr-001--graph-storage-adjacency-list--recursive-cte-in-postgres) | Graph storage = adjacency list + recursive CTE in Postgres | Accepted |
| [ADR-002](#adr-002--vectors-are-rag-delegated-no-app-local-pgvector) | Vectors are RAG-delegated; no app-local pgvector | Accepted |
| [ADR-003](#adr-003--copilot-calls-llms-gateway--guardrails-directly-with-a-seam-to-xagent) | Copilot calls llms-gateway + guardrails directly (seam to xAgent) | Accepted |
| [ADR-004](#adr-004--mcp-is-a-separate-stateless-facade-mcp-eng-memory) | MCP is a separate stateless facade (`mcp-eng-memory`) | Accepted |
| [ADR-005](#adr-005--the-knowledge-corpus-must-not-live-in-the-memory-service) | The knowledge corpus must NOT live in the Memory service | Accepted |
| [ADR-006](#adr-006--pin-an-explicit-embedding-model-never-the-embed-alias) | Pin an explicit embedding model, never the `embed` alias | Accepted |
| [ADR-007](#adr-007--edges-and-entities-are-bitemporal) | Edges and entities are bitemporal | Accepted |
| [ADR-008](#adr-008--tenancy--one-org-shared-graph--app-owned-per-repoteam-acls) | Tenancy = one-org shared graph + app-owned per-repo/team ACLs | Accepted |

---

## ADR-001 — Graph storage = adjacency list + recursive CTE in Postgres

**Status:** Accepted · **Owner:** data + retrieval

### Context

The product is *fundamentally* a graph: "who owns `owner/name`", "what breaks if I change this service", "who are the experts on X", "why was this built". These are reachability and neighborhood questions over a typed, directed engineering graph. The platform already standardizes on PostgreSQL with a **frozen `pgvector/pgvector:pg16` image**, a per-service runtime role that **cannot `CREATE EXTENSION`** (extensions are created only by the migration role), and Contract-13 RLS tenant isolation. The graph is the crown jewel; it must be queryable transactionally alongside the rest of the app's state, must be tenant-isolated by the same RLS mechanism as every other table, and must not require operating a second datastore for the MVP.

### Decision

Store the graph as a **plain adjacency list inside Postgres** and traverse it with **recursive CTEs**, behind a swappable `GraphRetriever` seam. Concretely, in schema `cypherx_a1`:

- **`entities`** — graph nodes. `entity_id UUID PK`, `kind` (CHECK-constrained to the nine kinds `person|service|repo|feature|decision|incident|pr|ticket|document`), `natural_key`, `title`, `search_text`, `attrs JSONB`, `vector_ref JSONB`, and a generated **`fts tsvector`** column (`to_tsvector('english', title || ' ' || search_text)`) with a GIN index `idx_entities_fts`.
- **`edges`** — typed directed relationships as `(src_entity_id, dst_entity_id, rel)` rows. `rel` is CHECK-constrained to the eleven relation types (`owns, authored, reviewed, depends_on, caused, resolved, mentions, decided_in, deployed, expert_in, part_of`), with `confidence NUMERIC(4,3)`, `extractor_version`, and `evidence_chunk_ids UUID[]`.

Traversal is **bidirectional adjacency-list walking** indexed by the two leading-tenant composite indexes:

```
idx_edges_src ON edges (tenant_id, src_entity_id, rel)
idx_edges_dst ON edges (tenant_id, dst_entity_id, rel)
idx_edges_current ON edges (tenant_id, src_entity_id, rel) WHERE valid_to IS NULL  -- partial, current slice
```

Multi-hop queries are **recursive CTEs** over the current edge slice. The shipped `db/graph_repo.py` proves the pattern — e.g. `impact_of()` ("what breaks if changed") is a reverse-`depends_on` blast radius:

```sql
WITH RECURSIVE blast AS (
    SELECT e.src_entity_id AS entity_id, 1 AS depth
      FROM cypherx_a1.edges e
     WHERE e.dst_entity_id = %(eid)s AND e.rel = 'depends_on' AND e.valid_to IS NULL
    UNION
    SELECT e.src_entity_id, b.depth + 1
      FROM cypherx_a1.edges e
      JOIN blast b ON e.dst_entity_id = b.entity_id
     WHERE e.rel = 'depends_on' AND e.valid_to IS NULL AND b.depth < %(max_hops)s
)
SELECT n.entity_id, ..., min(b.depth) AS depth FROM blast b JOIN cypherx_a1.entities n ...
```

The same module ships `neighbors()` (one-hop typed, `out|in|both`), `owners_of()`, `experts_on()` (a CTE that FTS-matches topic nodes then aggregates ownership-ish edges), and `find_entities()` / `keyword_search()` (FTS legs). **Adjacency-list + recursive-CTE is mandatory**; any future backend must hide behind the `GraphRetriever` seam without changing the API.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Apache AGE** (Postgres graph extension, openCypher) | The runtime role **cannot `CREATE EXTENSION`** and the image is **frozen** — AGE simply cannot be installed in this deployment model. It also adds a Cypher surface and operational footprint we don't need at MVP scope; recursive CTEs cover every shipped query. |
| **Neo4j** (dedicated graph DB) | A second datastore: separate ops, backups, its own auth/tenancy model, and **no RLS** — we would have to re-implement Contract-13 tenant isolation by hand and lose transactional consistency with `raw_events`, `extraction_jobs`, `citations`, and the `outbox`. Cross-store consistency (graph write + outbox event in one transaction) becomes impossible. |
| **`ltree`** (Postgres hierarchical path type) | Models *trees*, not a general directed multigraph. The engineering graph has many-to-many, cyclic, multi-typed edges (a service `depends_on` many services; a person `owns`/`authored`/`reviewed` the same repo). `ltree` cannot represent typed edges or reverse-dependency blast radius. |

### Consequences

- **Pros.** One datastore; the graph is tenant-isolated by the *same* RLS policy as everything else (`USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)`, `FORCE ROW LEVEL SECURITY`); graph writes and `outbox` events commit in one transaction; no extension and no second system to operate.
- **Cost.** Recursive CTEs are bounded by `max_hops` (default `retrieval_max_hops=3`, MCP caps `what_breaks_if_changed` at 6) and `LIMIT` to keep traversal cheap; very deep/dense traversals are deliberately *not* a first-cycle use case. If graph scale ever outgrows Postgres CTEs, the `GraphRetriever` seam is the single place to swap in a native graph engine — but **adjacency-list + recursive-CTE is the mandated default** and AGE/Neo4j/`ltree` remain rejected.
- **Discipline.** Every `graph_repo` function runs inside an `in_tenant()` transaction and never opens its own transaction or sets `app.tenant_id` itself — RLS scoping is the caller's responsibility, which keeps the repo composable and the isolation guarantee uniform.

---

## ADR-002 — Vectors are RAG-delegated; no app-local pgvector

**Status:** Accepted · **Owner:** retrieval

### Context

Hybrid retrieval needs a dense/semantic leg. The frozen image *is* `pgvector/pgvector:pg16`, so the temptation is to store embeddings in a local `vector` column and run ANN in-process. But the platform already owns a **Universal RAG service** (`Shared Core/rag`) whose entire job is the vector/semantic corpus: per-tenant knowledge bases, ingestion, pgvector ANN, ACLs, and a single owner of embedding cost. Two embedding stores means two embedding-cost owners, two ANN tuning surfaces, two ACL models, and drift between them.

### Decision

**Delegate all vectors to the SharedCore RAG service.** cypherx-a1 stores **only a pointer**, never an embedding. The `entities` table carries `vector_ref JSONB` documented as `{kb_id, doc_id, chunk_id}` — a reference into RAG, not a vector. The app leases a fixed set of per-tenant KBs (`eng-code`, `eng-conversations`, `eng-docs`, `eng-incidents`), inline-ingests normalized engineering text (≤100 KiB) via `POST /v1/kbs/{kb_id}/documents`, and queries `POST /v1/kbs/{kb_id}/query` for the dense leg. The shipped `services/rag_client.py` is the only place embeddings happen for the corpus, and the module docstring states the invariant directly: *"The GRAPH never enters RAG (rag.chunks are opaque text+JSONB)."*

The dense leg is one of three legs fused in `retrieval/orchestrator.py`: **graph** (`find_entities`), **rag-dense** (`RagClient.query` across the KBs resolved from `rag_kbs`), and **keyword** (`keyword_search`, a second tsvector pass — RAG ships dense-only first cycle, so cypherx-a1 owns the keyword leg). RAG hits are mapped back to their originating graph entity via the `doc_id` → entity link (`citations` / `ingest_repo.entities_for_docs`) so a chunk and its entity reinforce each other in RRF.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **App-local pgvector column** on `entities` | Creates a *second* embedding-cost owner and a *second* ANN tuning surface, duplicating exactly what RAG exists to centralize. It also couples graph storage to embedding-model dimensionality (re-embedding on a model change becomes a schema migration). RLS + ANN index maintenance in the app's own schema is avoidable operational weight. |
| **Run our own ANN service** | Same downsides, plus a new datastore — contradicts the consuming-app principle of reusing SharedCore over `/v1`. |

### Consequences

- **Pros.** Single embedding-cost owner (RAG), single ANN surface, single corpus ACL model; the graph schema stays embedding-agnostic — `vector_ref` is just a JSONB pointer, so an embedding-model change never touches `entities`. Hybrid fusion, keyword, rerank, and query expansion stay **app-side** (the orchestrator owns RRF and the tsvector keyword leg), which is the right division of labor: RAG ships dense-only first cycle and cypherx-a1 makes it hybrid.
- **Cost / constraints.** Query-time clamps are honored on every call: `top_k ≤ 100`, `ef_search ≤ 500` (the client enforces `min(top_k, 100)` and `min(ef_search, 500)`), `@>`-containment filters only (range/time filtering is done app-side over ISO strings). Ingest is inline-only and bounded at ≤100 KiB. A RAG `403` ACL deny degrades gracefully (`RagQueryResult.forbidden=True`, the leg is skipped) rather than failing the whole answer.
- **Consume additively.** cypherx-a1 consumes RAG strictly via `/v1` with additive-field tolerance and never pushes business logic into RAG.

---

## ADR-003 — Copilot calls llms-gateway + guardrails directly (seam to xAgent)

**Status:** Accepted · **Owner:** copilot

### Context

The copilot (`POST /v1/copilot/ask`) must screen input, retrieve cited context, call a model, screen output, and remember the turn. The platform's *eventual* canonical path for agent execution is **through xAgent** (which runs `LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → EVENT`). But cypherx-a1's copilot has app-specific retrieval (hybrid graph+RAG+keyword RRF) and citation semantics that xAgent does not model today. Routing through xAgent now would mean either bending xAgent to this app's retrieval or shipping a degraded copilot.

### Decision

For the MVP, the copilot calls **llms-gateway and guardrails directly**, with a **clean seam to route through xAgent later**. `copilot/service.py` implements the exact stage order, mirroring xAgent's pipeline so the future migration is mechanical:

```
memory recall → PRE-guardrail(question) → hybrid retrieve → prompt build →
llms chat → POST-guardrail(answer, input_text=question) → store episodic memory → cited answer
```

Key behaviors, as shipped:

- **llms-gateway is the only path to a provider.** `LlmsClient.chat()` posts to `POST /v1/chat/completions`; the gateway owns cost metering. The client preserves `llm_call_id` and `usage.cost_usd` from the gateway response and **never rewrites them** — `llm_call_id` is the billing key (Contract 19). `Idempotency-Key` is supported so a retried extraction worker replays the gateway result instead of re-spending.
- **Guardrails are fail-closed.** `GuardrailsClient.check_input` (`/v1/check/input`) runs *before* retrieval; `check_output` (`/v1/check/output`, passing the original `input_text=question` so echoed PII is distinguishable) runs *after* generation. `decision == "block"` raises `ApiError(GUARDRAIL_VIOLATION)` → **HTTP 422**; `decision == "redact"` swaps in `processed_text`. A 5xx/invalid guardrail decision raises (closed, not open).
- **Memory is best-effort.** A memory outage never fails an answer (reads return `[]`, writes swallow errors).
- **Identity is header-only.** Every downstream call carries `Authorization: Bearer <service JWT>` (Contract-12 service token) + `X-Forwarded-Agent-JWT: <agent jwt>` + W3C trace headers; **bodies carry no identity** (Contract 13).

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Route copilot through xAgent now** | xAgent does not model this app's hybrid retrieval or citation semantics; integrating now would either degrade the copilot or push app logic into xAgent (violating the consuming-app boundary). The seam keeps the *option* open without paying for it prematurely. |
| **Call providers directly (bypass llms-gateway)** | Forbidden by platform invariant — llms-gateway is the single choke point for cost metering, normalization, and provider abstraction. cypherx-a1 must never be a second cost-metering owner. |
| **Skip guardrails for "internal" agents** | The copilot answers free-text questions and emits LLM output to humans and agents; fail-closed pre/post screening is mandatory. There is no trusted-internal exemption. |

### Consequences

- **Pros.** The copilot ships with full hybrid retrieval and citations today; the stage order is xAgent-shaped, so re-pointing the LLM+guardrail stages through xAgent later is a localized change. Cost stays correctly attributed (gateway-owned `llm_call_id`).
- **Cost.** Two guardrail round-trips per answer (input + output) plus one gateway round-trip; the retrieval legs run before the LLM. This is the price of fail-closed safety + citation grounding and is accepted.
- **Seam contract.** The "seam" is the `CopilotService` boundary: it depends on `GuardrailsClient` / `LlmsClient` / `MemoryClient` / `RetrievalOrchestrator` by interface, so swapping the LLM+guardrail legs for an xAgent task call does not touch retrieval or citation code.

---

## ADR-004 — MCP is a separate stateless facade (`mcp-eng-memory`)

**Status:** Accepted · **Owner:** MCP / tools

### Context

Autonomous coding agents must be able to query engineering memory over **MCP** (Contract 4). The product API (`/v1/graph/*`, `/v1/copilot/ask`) already answers every question an agent needs. The question is whether to bolt an MCP surface onto the main service or ship a separate server. The main service is stateful (owns the graph, the DB pool, the outbox publisher, the Kafka worker); an MCP tool server should be cheap, horizontally scalable, and carry no state.

### Decision

Ship **`mcp-eng-memory` as a separate, stateless facade** (its own package `mcp_eng_memory`, host port **8094**, registered in Tool Registry as `mcp-eng-memory@1.0.0`). It exposes Contract-4 `POST /mcp/v1/invoke` and a static `manifest.json`, and it **proxies to the cypherx-a1 product API** — it owns no graph, no DB, no tenancy logic.

- **Seven read-only, source-citing tools** (`manifest.json`): `who_owns`, `why_built`, `what_breaks_if_changed`, `experts_on`, `graph_neighbors` (graph proxies → `/v1/graph/*`), and `incident_root_cause`, `how_does_x_work` (LLM proxies → `/v1/copilot/ask`). `api/invoke.py::_dispatch` maps each tool name to a backend call.
- **Stateless proxy.** `services/backend.py::BackendClient` forwards the resolved agent JWT (`Authorization: Bearer <agent_jwt>`) + W3C trace to cypherx-a1; *"cypherx-a1 re-verifies the agent JWT and enforces tenant RLS, so this facade carries no tenant logic of its own."* A backend `401/403` surfaces as `FORBIDDEN`.
- **Same logic, two front doors.** `api/graph.py`'s docstring is explicit: the `/v1/graph/*` endpoints are *"ALSO the backing API the stateless `mcp-eng-memory` server proxies, so the same logic serves both the public REST API and autonomous coding agents over MCP."* No query logic is duplicated in the facade.
- **Metering is the caller's, never the tool's.** `api/invoke.py` states it directly: *"This server is stateless: NO metering is emitted here — the calling agent's (xAgent) outbox owns per-invocation metering."* The facade enforces fine scope (`tool:mcp-eng-memory:invoke` in addition to coarse `tool:invoke`), a request body-size cap, JSON-Schema input validation (422 + JSON Pointer), and an output-size cap.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Embed the MCP surface in the main service** | Couples a stateless, horizontally-scalable tool surface to the stateful service (DB pool, outbox, Kafka worker); they have different scaling and failure profiles. A noisy MCP load would contend with ingestion/extraction. |
| **Re-implement queries in the MCP server** | Duplicates graph/copilot logic and invites drift. The facade proxies the same `/v1` endpoints so there is exactly one implementation. |
| **Emit metering from the tool** | Violates the platform rule that **per-invocation tool metering is the caller's (xAgent's) outbox**, not the stateless tool server's. A stateless tool with no outbox cannot own billing. |

### Consequences

- **Pros.** The MCP server scales independently and statelessly; tenant isolation and JWT re-verification happen exactly once (in cypherx-a1), so the facade can never leak across tenants. One query implementation serves REST and MCP.
- **Cost.** One extra network hop (agent → MCP facade → cypherx-a1). Accepted for the isolation and scaling benefits.
- **Boundary.** Identity flows agent JWT → facade → backend unchanged; the facade adds scope/size/schema guards but **no business logic**, keeping it a true facade.

---

## ADR-005 — The knowledge corpus must NOT live in the Memory service

**Status:** Accepted · **Owner:** copilot / data

### Context

The Memory service stores principal-scoped, episodic agent memory. It is tempting to treat the entire engineering knowledge corpus as "memory" and store it there. But Memory is **per-principal** and its embeddings are billed per write; the engineering corpus is **organization-shared** and large. Putting the corpus in Memory would either leak one principal's view to another (cross-principal leakage) or re-embed the whole corpus per principal (cost explosion) — and would conflate two fundamentally different lifetimes (a conversation turn vs. the permanent record of how the org was built).

### Decision

**The knowledge graph and corpus live in the app-owned graph (`cypherx_a1.entities` / `edges`) + RAG (the vector corpus). The Memory service is used ONLY for the copilot's conversational working memory.** `services/memory_client.py` is scoped accordingly and says so: *"This is NOT where the engineering knowledge corpus lives (that is the graph + RAG)."* In the copilot flow:

- Memory **search** (`/v1/memories/search`, `include_shared: false`) recalls up to 3 prior turns for conversational continuity, gated on `copilot_memory_enabled`.
- Memory **store** (`/v1/memories`, `scope: "principal_only"`, `type: "episodic"`) records `Q:/A:` after each answer, keyed by `idempotency_key`.
- Sessions are registered best-effort via `/v1/sessions`.

All of this is **per-principal episodic** and **best-effort** (an outage never fails an answer). The corpus itself — the durable, org-shared knowledge — is never written here.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Store the corpus in Memory** | Memory is per-principal: either it leaks across principals (security) or it re-embeds the org-shared corpus per principal (cost). Both are disqualifying. The corpus belongs in the shared graph + RAG. |
| **Store conversational turns in the graph/RAG** | Conversational turns are ephemeral, per-principal context, not durable org knowledge; mixing them into the shared corpus pollutes retrieval and tenancy. |

### Consequences

- **Pros.** Clean lifetime separation: durable org knowledge in graph+RAG (ADR-001, ADR-002), ephemeral per-principal context in Memory. No cross-principal leakage; embedding cost for the shared corpus is paid once (via RAG), not once-per-principal.
- **Guardrail.** A lint guard (referenced in `memory_client.py` and `docs/02-sharedcore-integration-boundary.md`) keeps the corpus out of Memory; this ADR is the rationale that guard enforces.
- **Cost.** Conversational recall adds a best-effort memory round-trip per answer; it is non-blocking by design.

---

## ADR-006 — Pin an explicit embedding model, never the `embed` alias

**Status:** Accepted · **Owner:** retrieval / data

### Context

RAG exposes a repointable `embed` alias that can be redirected to whatever the platform's current default embedding model is. If cypherx-a1 created its KBs against that alias, a platform-side repoint of `embed` would silently change the embedding space *between* KBs and *over time* — making vectors written before and after the change non-comparable, and making different KBs (`eng-code` vs `eng-incidents`) potentially live in different spaces. Dense retrieval quality would degrade invisibly.

### Decision

**Create every KB with an explicit, pinned embedding model name — never the `embed` alias — and persist the resolved binding.** `core/config.py` pins `rag_embedding_model = "text-embedding-3-small"` and `rag_embedding_dim = 1536` (the only platform-supported dimension). `rag_client.py::create_kb` passes the explicit model as `embedding_model_alias` *"so RAG resolves it to a stable literal rather than the repointable 'embed' default."* The resolved binding is recorded immutably in **`cypherx_a1.rag_kbs`**:

```
rag_kbs (tenant_id, logical_name, kb_id, embedding_model_resolved, embedding_dim, created_at)
PRIMARY KEY (tenant_id, logical_name)
```

`embedding_model_resolved` is the literal RAG resolved at creation; `(tenant_id, logical_name)` is the stable lookup the orchestrator uses (`_list_kb_ids` reads `kb_id` from `rag_kbs`). The four logical KBs are `eng-code`, `eng-conversations`, `eng-docs`, `eng-incidents` — all created with the same pinned model, so they share one embedding space.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Use the `embed` alias** | A platform repoint of `embed` silently changes the embedding space; old and new vectors stop being comparable and KBs can diverge. Retrieval quality degrades with no schema change to signal it. Disqualifying for a corpus that must stay self-consistent. |
| **Pin per-KB different models** | Cross-KB fusion (RRF over hits from multiple KBs) assumes one comparable space; different models per KB break that assumption. One pinned model across all four KBs is the invariant. |

### Consequences

- **Pros.** All KBs share one stable, comparable embedding space; the resolved model is recorded in `rag_kbs` for auditability; a future model migration is an explicit, deliberate operation (new pin → re-ingest), not a silent drift.
- **Cost / constraint.** Changing the pin requires re-ingesting the corpus into freshly-pinned KBs; this is intentional friction that protects retrieval correctness. `embedding_dim` is fixed at 1536 (platform-supported), so the `vector_ref` pointers and dimensionality stay uniform.

---

## ADR-007 — Edges and entities are bitemporal

**Status:** Accepted · **Owner:** data / extraction

### Context

Engineering reality changes: ownership transfers, dependencies are added and removed, decisions are superseded. LLM extraction is *probabilistic* and *versioned* — re-running a better extractor over the same content should **supersede** old conclusions, not duplicate or destroy them. "Who owned this service *at the time of the incident*" and "what did we believe before re-extraction" are real questions. A point-in-time-only graph cannot answer them and cannot safely re-extract.

### Decision

Both `entities` and `edges` are **bitemporal**: every row carries `valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW()` and `valid_to TIMESTAMPTZ` where **`valid_to IS NULL` means the current version**. Closing a fact sets `valid_to = NOW()` instead of deleting it. The **current slice** is what every read query filters to, backed by partial indexes:

- `entities`: partial **unique** index `uq_entities_natural_current (tenant_id, kind, natural_key) WHERE valid_to IS NULL` — exactly one current row per `(tenant, kind, natural_key)`. `upsert_entity` conflicts on this index and updates in place, so re-ingest keeps a **stable `entity_id`** (edges and citations stay valid).
- `edges`: partial index `idx_edges_current (tenant_id, src_entity_id, rel) WHERE valid_to IS NULL`. `upsert_edge` supersedes-in-place the current edge for `(src, dst, rel)`.

Re-extraction is bitemporally safe. `supersede_extracted_edges(src_entity_id, extractor_version)` closes the *current* extracted edges (`extractor_version <> 'ingest'`) that predate a new extractor version:

```sql
UPDATE cypherx_a1.edges SET valid_to = NOW()
 WHERE src_entity_id = %s AND valid_to IS NULL
   AND extractor_version <> 'ingest' AND extractor_version <> %s
```

This is paired with the `extraction_jobs` cost ledger (PK `(tenant_id, node_id, content_sha, extractor_version)` with `llm_call_id`, `cost_usd`): bumping `extractor_version` (config default `"1.0.0"`) supersedes prior extracted edges **without re-spending on unchanged content** — the ledger keys on the version, so an unchanged `content_sha` at the same version is a no-op.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Mutate/delete rows in place (no history)** | Destroys "what we believed before" and makes re-extraction lossy and unsafe; can't answer point-in-time questions; a bad extractor pass is irreversible. |
| **Separate history/audit tables** | Doubles the write path and splits "current" and "historical" reads across tables; the partial-index-on-current-slice pattern gives both from one table with a single `WHERE valid_to IS NULL`. |

### Consequences

- **Pros.** Re-ingest and re-extraction are idempotent and non-destructive; `entity_id` is stable across re-ingest so edges/citations never dangle; point-in-time history is retained; the cost ledger prevents re-spend.
- **Cost.** Rows accumulate over time (superseded versions are retained); current-slice reads stay fast via the partial indexes, but long-term a retention/compaction policy for closed rows may be needed. Reads must always remember `WHERE valid_to IS NULL` — this is the universal convention across `graph_repo`.

---

## ADR-008 — Tenancy = one-org shared graph + app-owned per-repo/team ACLs

**Status:** Accepted · **Owner:** auth / data

### Context

A tenant is an **organization**. Within an org, everyone should be able to reason over the org's engineering memory, but **not every engineer may read every private repo or team's data**. SharedCore Auth authenticates *agents* and never models repos or teams — it has no concept of "the payments repo" or "the platform team". So authorization at repo/team granularity has to live *somewhere*; the only correct owner is the app that knows what a repo or team is.

### Decision

**One tenant per org, one shared graph, plus app-owned per-repo/team ACLs.** Tenant isolation is the platform-standard Contract-13 RLS: every tenant-scoped table (`entities, edges, identities, raw_events, connectors, connector_secrets, sync_cursors, extraction_jobs, citations, resource_acls, rag_kbs`) has `ENABLE` + `FORCE ROW LEVEL SECURITY` and an `_isolation` policy `USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)`. The runtime role `cxa1_user` is **not a superuser and does not BYPASSRLS**; the `outbox` is the one table with **no RLS** (it's an internal cross-tenant publish queue drained by a background task that sets no `app.tenant_id` — isolation lives in the payload's `partition_key=tenant_id`, not the row).

**Finer-grained authorization is app-owned**, in `cypherx_a1.resource_acls`:

```
resource_acls (acl_id, tenant_id,
  resource_type   -- repo | team | service
  resource_key    -- e.g. "owner/name"
  principal_type  -- agent | user | role | tenant   (CHECK-constrained)
  principal_id    -- id or '*'
  permission      -- default 'read'
)
UNIQUE (tenant_id, resource_type, resource_key, principal_type, principal_id)
```

The migration comment is explicit: *"App-owned authorization on engineering entities. Auth never models repos/teams."* The app, not Auth, decides who may read `owner/name`. SharedCore involvement is limited to identity: Auth verifies the inbound agent JWT (JWKS) and mints the Contract-12 service token; the app's own `0002__seed.sql` seeds the `auth.service_acl` edges (canonical columns `caller_service, target_service, allowed_scopes`) so cypherx-a1 may mint service tokens for the SharedCore services it calls — **without modifying SharedCore/auth**.

### Alternatives rejected

| Option | Why rejected |
|--------|--------------|
| **Per-user/per-repo separate tenants** | Shatters the shared org graph — cross-repo reasoning ("what breaks across the org if I change X") becomes impossible, and re-creates the corpus per slice. The org-shared graph is the product's value. |
| **Model repos/teams in Auth** | Auth authenticates agents and is deliberately repo/team-agnostic; pushing repo/team ACLs into Auth would put app business logic into SharedCore, violating the consuming-app boundary. The app owns what only the app understands. |
| **No sub-tenant ACLs (everyone sees everything)** | Private repos and sensitive team data demand read restrictions; a flat "all org members see all" model is not acceptable for real engineering orgs. |

### Consequences

- **Pros.** Org-wide reasoning works (one shared graph) while private resources stay protected (`resource_acls`); tenant isolation is the same battle-tested RLS as every other service; SharedCore stays untouched (auth.service_acl seeded from the app's own migration). RAG KB ACL denies (`403 → forbidden`) compose with app ACLs as a second layer.
- **Cost / responsibility.** The app owns resource-level authorization correctness — ACL checks must be applied on the read paths that expose repo/team-scoped data, and `resource_acls` must be kept in sync with connector config (which repos/teams exist). Connectors, ACLs, and KB bindings are created **per-tenant at runtime** via the API, not seeded.

---

## Cross-cutting invariants these ADRs share

These hold across all eight decisions and are the platform-alignment guarantees the app is built to:

| Invariant | Where it shows up |
|-----------|-------------------|
| **Identity in headers, never bodies** | Contract-12 service token in `Authorization` + `X-Forwarded-Agent-JWT` + W3C trace on every downstream call (`rag_client`, `llms_client`, `memory_client`, `guardrails_client`); request bodies carry no identity (Contract 13). |
| **Tenant isolation by RLS** | `app.tenant_id` set per transaction via `in_tenant()`; `FORCE ROW LEVEL SECURITY` on every tenant table; `cxa1_user` has no BYPASSRLS; `outbox` deliberately has no RLS. |
| **SharedCore consumed over `/v1`, additively** | No business logic pushed into SharedCore; additive-field tolerance; reserved JWT claims (`cnf, wkl_id, behavior_policy_id, delegation_*, approval_context`) are accepted-but-ignored for Phase-13. |
| **Cost owned by the gateway** | `llm_call_id` + `usage.cost_usd` from llms-gateway are never rewritten; `Idempotency-Key` prevents re-spend; the `extraction_jobs` ledger keys on `(node_id, content_sha, extractor_version)`. |
| **Outbox + Contract-5 eventing** | `cypherx_a1.outbox` (partition_key = tenant_id) drained to `cypherx.cypherxa1.*` topics; the app consumes only `cypherx.tenant.*`; metering is the caller's, never the tool's. |
| **Fail-closed safety** | Guardrails block → `422 GUARDRAIL_VIOLATION`; invalid/5xx guardrail decisions raise (closed); Memory and RAG-403 degrade gracefully (availability) but safety never opens. |
| **Don't break Contract-15 cases 1–10** | The app is a peer of ax-1 and must not regress the first-cycle spine. |

### Source map (where to verify each ADR)

| ADR | Primary code / schema |
|-----|------------------------|
| 001 graph | `db/migrations/20260614_0001__init.sql` (`entities`, `edges`, indexes); `src/cypherx_a1/db/graph_repo.py`; `src/cypherx_a1/api/graph.py` |
| 002 vectors | `entities.vector_ref`, `rag_kbs`; `src/cypherx_a1/services/rag_client.py`; `src/cypherx_a1/retrieval/orchestrator.py` |
| 003 copilot | `src/cypherx_a1/copilot/service.py`; `src/cypherx_a1/api/copilot.py`; `services/llms_client.py`, `services/guardrails_client.py` |
| 004 MCP | `mcp-eng-memory/manifest.json`; `mcp-eng-memory/src/mcp_eng_memory/api/invoke.py`; `.../services/backend.py` |
| 005 memory | `src/cypherx_a1/services/memory_client.py` |
| 006 embedding pin | `src/cypherx_a1/core/config.py` (`rag_embedding_model`, `rag_embedding_dim`); `rag_kbs`; `rag_client.create_kb` |
| 007 bitemporal | `entities`/`edges` `valid_from`/`valid_to`, partial indexes; `graph_repo.upsert_*`, `supersede_extracted_edges`; `extraction_jobs` |
| 008 tenancy | RLS block + `resource_acls` in `20260614_0001__init.sql`; `db/migrations/20260614_0002__seed.sql` (auth.service_acl) |
