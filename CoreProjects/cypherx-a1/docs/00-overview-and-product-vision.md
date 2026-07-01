# Overview & product vision

> **cypherx-a1 — Autonomous Engineering Memory.** Continuously ingest an organisation's engineering sources (GitHub first), normalize them into a tenant-scoped knowledge **graph** of Person/Service/Repo/Feature/Decision/Incident/PR/Ticket/Document, enrich it with LLM knowledge-extraction, and answer "who owns this / why was it built / what breaks if I change it / who's the expert / what caused this incident" through a **cited AI copilot** and a **stateless MCP server** that any AI coding agent can call. It is a *consuming app* on the CypherX platform — a peer of `xAgent/ax-1`, not a SharedCore service.

---

## 1. The problem: engineering knowledge loss

Every engineering organisation past ~10 engineers leaks knowledge faster than it can write it down. The `base-idea.md` framing names the failure precisely:

- **Senior engineers leave**, and the *why* leaves with them.
- **Documentation is outdated** the moment it is written; nobody is paid to keep it fresh.
- **New hires take months** to become productive because the truth is distributed.
- **Knowledge lives only in Slack, Jira, GitHub, PR comments, and people's heads** — never in one queryable place.

The incumbent answers — **Confluence, Notion, GitBook** — are all *manual wikis*. They share one structural defect: they require a human to write and re-write them. So they go stale, the team stops trusting them, and the team stops reading them. A wiki is a snapshot; an engineering org is a stream.

The market is large and obvious: **every company with more than ~10 engineers** has this pain, continuously, forever. The opportunity is not "a better wiki" — it is removing the human-maintenance step entirely.

> **The thesis:** the source systems (Git history, PR reviews, tickets, incidents, chat) *already contain* the knowledge. The job is not to ask humans to re-author it — it is to **continuously read those systems, extract the relationships, and serve them back with citations**. Living memory, not dead documentation.

---

## 2. The base idea, critiqued and improved

`base-idea.md` sketches a clean seven-layer pipeline: **Data Sources → Ingestion → Normalization → Knowledge Extraction → Storage (graph + vector + metadata) → Reasoning (hybrid retrieval) → AI Copilot → Output (cited answers)**. That skeleton is sound and cypherx-a1 keeps it. But a sketch is not an architecture. The improvements below are what turn the idea into something that survives contact with a real multi-tenant platform.

| # | Base idea (as drawn) | Problem if taken literally | cypherx-a1 improvement |
|---|----------------------|----------------------------|------------------------|
| 1 | "GitHub + Jira + Slack + Confluence + PagerDuty + CI/CD" all at once | Six connectors, six auth models, six webhook formats — a year before the first answer ships | **GitHub-first MVP.** One connector, proven end-to-end. The canonical model (`CanonicalRecord` → nodes/edges/docs) is connector-agnostic so the rest are *additive*, not rewrites. |
| 2 | "Graph Database" (e.g. Neo4j) | A second datastore to operate, secure, back up, and tenant-isolate; and it would sit *outside* the platform's Postgres RLS story | **Graph in Postgres** as an adjacency list (`entities` + `edges`) with recursive-CTE traversal, behind a `GraphRetriever` seam. RLS, backups, and migrations are the platform's existing ones. Swappable later without touching callers. |
| 3 | "Vector Database" as a peer store this app owns | Duplicates embedding infrastructure the platform already has; risks the graph and the vectors drifting | **Vectors are delegated to SharedCore RAG.** This app stores only a `vector_ref` (`{kb_id, doc_id, chunk_id}`) on each entity. **The graph never enters RAG.** One embedding stack, one bill. |
| 4 | "LLM" as a box at the bottom | A direct provider key in the app means no metering, no guardrails, no cost ceiling, no central model policy | **Every LLM call goes through `llms-gateway`** (extraction + copilot answers). `llm_call_id` is the billing key; cost is never rewritten. Embeddings are reached *indirectly* through RAG. |
| 5 | "Hybrid Retrieval = graph + semantic + keyword" (hand-wave) | "Hybrid" is the hard part; "combine three rankings" is undefined | **Reciprocal-Rank Fusion (RRF)** with an explicit constant (`retrieval_rrf_k=60`), RAG hits mapped back to their originating entity via the `doc_id` citation link so a chunk and its entity *reinforce* each other. Keyword/RRF/rerank/expansion are **owned app-side** (RAG ships dense-only first cycle). |
| 6 | "Answers with citations" as an output feature | Bolted-on citations are unverifiable and easy to fabricate | **Citations are the unit of retrieval, not a post-hoc decoration.** Every surviving evidence item *becomes* a `Citation`; the copilot can never return an uncited answer. An autonomous agent consuming the result can re-fetch the source. |
| 7 | A chat UI as the only consumer | Humans-in-a-browser is the *small* market in 2026; the big one is **other agents** | A **stateless MCP server** (`mcp-eng-memory@1.0.0`) exposes the same query logic as Contract-4 tools so AI coding agents query the memory directly. The copilot is one consumer; the MCP facet is the leverage. |
| 8 | Implicitly single-org / no tenancy | A SaaS with no isolation story is a breach waiting to happen | **One tenant per org, shared graph within the tenant, plus app-owned per-repo/team ACLs** (`resource_acls`). Postgres RLS (`FORCE ROW LEVEL SECURITY`, `app.tenant_id`) makes cross-tenant access architecturally impossible. |
| 9 | "Documentation is outdated" — but the system could be too | A point-in-time graph can't answer "who owned this *last quarter*" | **Bitemporal entities and edges** (`valid_from` / `valid_to`, NULL = current). History is retained; the current slice is a partial unique index. The memory can answer *as-of* questions. |

The net effect: the base idea's *value* is preserved verbatim, but every "box" that would have become a separate piece of infrastructure is instead expressed through the CypherX platform's existing SharedCore contracts. cypherx-a1 adds **engineering-domain logic**, not infrastructure.

---

## 3. The value proposition: autonomous engineering memory

cypherx-a1 sells one thing: **an organisation's engineering knowledge, kept current automatically, queryable with evidence, by humans and by agents.**

Three properties make it *autonomous* rather than another wiki:

1. **It maintains itself.** Connectors (GitHub-first) backfill and then track sources via webhooks + scheduled sync. Each delivery is normalized, deduped (`raw_events.content_sha`), upserted into the graph, embedded into RAG, and run through LLM knowledge-extraction. No human writes a doc.
2. **It reasons over structure, not just text.** Because the knowledge is a typed graph (`owns`, `authored`, `reviewed`, `depends_on`, `caused`, `resolved`, `decided_in`, `expert_in`, …), it can answer relationship questions — *blast radius*, *ownership*, *expertise* — that pure RAG cannot. It then fuses that graph with dense + keyword retrieval (RRF) so prose and structure reinforce each other.
3. **It is consumable by agents.** The same query logic that powers the human copilot is exposed as MCP tools, so an AI coding agent about to change a file can ask *"what breaks if I change this"* and *"who owns this"* before it acts — grounded, cited, and tenant-scoped.

The canonical questions cypherx-a1 answers (straight from `base-idea.md`, now backed by real endpoints):

| Question | Backing endpoint | MCP tool |
|----------|------------------|----------|
| Who owns this service / repo / file? | `POST /v1/graph/who-owns` | `who_owns` |
| Why was this built? | `POST /v1/graph/why-built` | `why_built` |
| What will break if I change this? | `POST /v1/graph/what-breaks` | `what_breaks_if_changed` |
| Who are the experts on X (e.g. Kafka)? | `POST /v1/graph/experts` | `experts_on` |
| What's connected to this entity? | `POST /v1/graph/neighbors` | `graph_neighbors` |
| What caused this incident? | `POST /v1/copilot/ask` (LLM) | `incident_root_cause` |
| How does authentication work? | `POST /v1/copilot/ask` (LLM) | `how_does_x_work` |

The first five are **read-only, no-LLM, structured-and-cited** graph queries (cheap, fast, deterministic). The last two route through the LLM copilot for synthesis. Both surfaces always carry citations.

---

## 4. Scope and non-scope

cypherx-a1 is a **consuming app** (peer of `xAgent/ax-1`). It reuses SharedCore strictly via versioned `/v1` contracts and pushes **no business logic into SharedCore**. Deliverable = **docs + a working MVP slice**.

### In scope (MVP slice)

| Area | What is in | Where |
|------|-----------|-------|
| **Ingestion** | GitHub connector (commits / PRs / reviews / issues), mock fixtures by default, live when configured; resumable (`sync_cursors`), idempotent (`raw_events`) | `connectors/github.py`, `ingestion/pipeline.py` |
| **Normalization** | Source records → `CanonicalRecord` (nodes + edges + RAG docs), graph upsert, RAG embed | `ingestion/normalizer.py`, `models/canonical.py` |
| **Knowledge extraction** | LLM pass over not-yet-extracted artifacts → typed edges; idempotent + cost-metered | `extraction/extractor.py`, `extraction_jobs` table |
| **Storage** | App-owned bitemporal graph in Postgres `cypherx_a1`; vectors delegated to RAG (`vector_ref`) | `db/graph_repo.py`, `20260614_0001__init.sql` |
| **Hybrid retrieval** | graph + RAG-dense + keyword, fused with RRF, fully cited | `retrieval/orchestrator.py` |
| **AI copilot** | `POST /v1/copilot/ask` — memory recall → pre-guardrail → retrieve → LLM → post-guardrail → cited answer | `copilot/service.py` |
| **Graph query surface** | `/v1/graph/*` — read-only, cited, no-LLM | `api/graph.py`, `copilot/queries.py` |
| **MCP facet** | Stateless `mcp-eng-memory@1.0.0`, 7 read-only tools, `POST /mcp/v1/invoke` | `mcp-eng-memory/` |
| **Tenancy** | One tenant per org, shared graph, app-owned per-repo/team ACLs | `resource_acls` table |
| **Eventing** | Transactional outbox → Kafka `cypherx.cypherxa1.*` (Contract-5 envelope, paired `.dlq`) | `db/outbox.py` |

### Out of scope (explicitly NOT this app's job)

| Not in scope | Why / where it lives instead |
|--------------|------------------------------|
| Running its own graph database (Neo4j/AGE) | Adjacency-list + recursive-CTE in Postgres is mandatory first cycle; runtime role cannot `CREATE EXTENSION` (frozen `pgvector/pgvector:pg16` image). |
| Owning a vector store / embedding model | **SharedCore RAG** owns vectors. This app stores only a `vector_ref`. The graph **never** enters RAG. |
| Calling LLM providers directly | **`llms-gateway`** is the only path to a provider, for both extraction and copilot. |
| Storing the knowledge graph in Memory | **SharedCore Memory** is copilot *conversational working memory only* (per-principal episodic). Putting the graph there would cause cross-principal leakage and unnecessary embedding cost. |
| Modeling repos/teams/users inside Auth | **Auth** verifies JWTs, mints service tokens, registers agents/keys. Per-repo/team authorization is **app-owned** (`resource_acls`). |
| Non-GitHub connectors (Jira/Slack/Confluence/PagerDuty/CI) | Designed-for but **not built** in the MVP. The canonical model makes them additive. |
| End-user identity / billing UI | Lives in the external `px0` system. cypherx-a1 authenticates **agents**, not end users. |
| Emitting per-tool metering from the MCP server | Metering is the **caller's (xAgent's) outbox**, never the stateless tool's (Contract-14). |
| Breaking platform invariants | Must not break Contract-15 cases 1–10; consumes only `cypherx.tenant.*` events; reserved JWT claims (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) are accepted-but-ignored for Phase-13. |

---

## 5. Glossary: entity / edge / graph vs RAG vs Memory

The single most important distinction in this codebase is **where a given piece of knowledge is stored and why**. Three storage planes, three jobs, no overlap.

| Term | What it is | Where it lives | Tenant model | What it answers |
|------|-----------|----------------|--------------|-----------------|
| **Entity** (node) | A typed thing in the knowledge graph: a Person, Service, Repo, Feature, Decision, Incident, PR, Ticket, or Document | `cypherx_a1.entities` (Postgres, app-owned) | RLS by `tenant_id` | "What things exist and what are they?" |
| **Edge** | A typed, directed, bitemporal relationship between two entities | `cypherx_a1.edges` (adjacency list, Postgres) | RLS by `tenant_id` | "How are they connected?" |
| **Graph** | Entities + edges traversed together (recursive CTE over the adjacency list) | Postgres `cypherx_a1` schema, behind a `GraphRetriever` seam | Shared within a tenant | "Who owns / who depends / who's the expert / what's the blast radius?" |
| **RAG** | The vector/semantic corpus: chunked text, embedded with a **pinned** model | **SharedCore RAG** (per-tenant KBs) | RAG-internal per-tenant isolation | "What text is *semantically* near this question?" |
| **Memory** | Per-principal episodic conversational working memory for the copilot | **SharedCore Memory** | Per-principal | "What did *this user* just ask me?" |

### Graph vs RAG vs Memory — the load-bearing rules

- **The graph is app-owned and NEVER enters RAG.** RAG holds *text chunks*; the graph holds *typed relationships*. An entity may reference a chunk via `vector_ref = {kb_id, doc_id, chunk_id}`, and a `citations` row links a RAG `doc_id` back to a graph `entity_id`/`edge_id` — but the graph structure itself is never embedded.
- **The graph is NEVER stored in Memory.** Memory is *per-principal* and *episodic*; the graph is *cross-principal* tenant knowledge. Mixing them would leak one user's graph into another's recall and burn embedding cost for no retrieval benefit.
- **Hybrid retrieval is the act of fusing graph + RAG-dense + keyword.** RRF mapping (`doc_id → entity`) is what makes a chunk and its entity reinforce each other — that reinforcement *is* the value of "hybrid" over plain RAG.

### Entity kinds (`entities.kind`, CHECK-constrained)

`person`, `service`, `repo`, `feature`, `decision`, `incident`, `pr`, `ticket`, `document`

### Edge relations (`edges.rel`, CHECK-constrained)

`owns`, `authored`, `reviewed`, `depends_on`, `caused`, `resolved`, `mentions`, `decided_in`, `deployed`, `expert_in`, `part_of`

### Other core terms

| Term | Meaning |
|------|---------|
| **Bitemporal** | Every entity/edge carries `valid_from` / `valid_to`; `valid_to IS NULL` is the current slice. History is never deleted, only superseded. |
| **`natural_key`** | The stable dedup key within `(tenant, kind)` — a repo's `owner/name`, a person's canonical login/email, a PR's `repo#number`. Lets edges wire two nodes before either has a UUID. |
| **`vector_ref`** | JSONB pointer `{kb_id, doc_id, chunk_id}` on an entity, into the RAG corpus. The graph stores the *reference*, RAG stores the *vector*. |
| **`CanonicalRecord`** | The connector-agnostic normalized shape: a set of nodes, typed edges, and embeddable RAG docs derived from one source record. The seam that makes new connectors additive. |
| **Citation** | The provenance unit on every answer/tool result: `{kind: entity\|chunk, title, source, uri, entity_id, doc_id, chunk_id, score, snippet, …}`. |
| **`resource_acls`** | App-owned per-repo/team/service read rules — the tenancy decision. Auth never models repos or teams. |
| **RRF** | Reciprocal-Rank Fusion: `score += 1 / (k + rank)` per leg, constant `k = 60`. The retrieval fusion algorithm. |

---

## 6. The three layers: product service, copilot, MCP facet

cypherx-a1 ships as **one backend with three consumption surfaces** over the same engineering-memory core. Two deployable processes back them:

- **`cypherx-a1`** — the product service (host `:8093`, in-container `:8080`).
- **`mcp-eng-memory`** — the stateless MCP server (host `:8094`, in-container `:8080`) that proxies the product service's graph + copilot endpoints.

```
                         ┌───────────────────────────────────────────────┐
                         │              cypherx-a1 core                  │
                         │  ingestion → graph (entities/edges) → RRF     │
                         │  hybrid retrieval (graph + RAG-dense + kw)    │
                         └───────────────────────────────────────────────┘
                                ▲                ▲                ▲
            Layer 1: Product    │   Layer 2:     │   Layer 3:     │
            service (REST)      │   Copilot      │   MCP facet    │
                                │                │                │
   /v1/connectors/{kind}/sync   /v1/copilot/ask   POST /mcp/v1/invoke (mcp-eng-memory)
   /v1/extract                  (LLM, guardrails,  tools: who_owns, why_built,
   /v1/graph/who-owns           memory, cited)     what_breaks_if_changed, experts_on,
   /v1/graph/what-breaks                            graph_neighbors, incident_root_cause,
   /v1/graph/why-built                              how_does_x_work
   /v1/graph/experts
   /v1/graph/neighbors
   /webhooks/{kind}  (HMAC-authed, no JWT)
```

### Layer 1 — the product service (ingestion + structured graph queries)

The CypherX-platform-native HTTP service. It owns the data plane and the deterministic query surface.

| Endpoint | Scope | Purpose |
|----------|-------|---------|
| `POST /v1/connectors/{kind}/sync` | `cypherxa1:ingest` | Pull a source (mock fixtures by default, live GitHub when configured) → normalize → graph upsert → RAG embed. Resumable, idempotent. |
| `POST /v1/extract` | `cypherxa1:ingest` | Run the LLM knowledge-extraction pass over not-yet-extracted artifacts. Idempotent + cost-metered (`extraction_jobs`). |
| `POST /v1/graph/who-owns` | `cypherxa1:query` | Owners/maintainers of a target, with evidence. **No LLM.** |
| `POST /v1/graph/what-breaks` | `cypherxa1:query` | Reverse-dependency blast radius (`max_hops`). **No LLM.** |
| `POST /v1/graph/why-built` | `cypherxa1:query` | The PRs/decisions/RFCs/tickets behind a feature. **No LLM.** |
| `POST /v1/graph/experts` | `cypherxa1:query` | People ranked by authored/reviewed/expert signal on a topic. **No LLM.** |
| `POST /v1/graph/neighbors` | `cypherxa1:query` | Typed neighbours of an entity (incoming + outgoing). **No LLM.** |
| `POST /webhooks/{kind}` | HMAC signature (no JWT) | App-owned webhook receiver. Graph-only landing; RAG embed deferred (a webhook carries no agent JWT to forward to RAG). |
| `GET /livez` · `GET /readyz` · `GET /metrics` | — | Contract-7 health/metrics. `readyz` gates on Postgres + warm Auth JWKS. |

Scopes are hierarchical (`core/auth.py`): `cypherxa1:admin` ⊇ `cypherxa1:ingest` ⊇ `cypherxa1:query`, plus the platform admin scopes (`agent:admin`, `platform:admin`). All request bodies are `extra="forbid"` — identity (`tenant_id`, `agent_id`, `trace_id`) comes **only** from the JWT; a body that tries to carry it is rejected with 422.

The graph query handlers return a `GraphAnswer` (`items` + `citations` + `trace_id`) — structured, deterministic, cheap. These exact handlers are what the MCP facet proxies.

### Layer 2 — the AI copilot (synthesized, cited answers)

`POST /v1/copilot/ask` is the human-facing question-answering surface. Per the locked decision it calls **`llms-gateway` + `guardrails` directly**, with a clean seam to route through xAgent later. The pipeline in `CopilotService.ask`:

```
memory recall  →  PRE-guardrail(question)  →  hybrid retrieve (RRF, cited)
   →  prompt build  →  llms chat  →  POST-guardrail(answer, input_text=question)
   →  store episodic memory  →  cited answer (AskResponse)
```

Guardrail discipline:

- **Fail-closed.** A guardrails 5xx or invalid decision raises; the answer never escapes screening.
- **`decision=block` → `422 GUARDRAIL_VIOLATION`** (input *or* output).
- **`decision=redact`** swaps in the processed text.
- The post-check passes `input_text=question` so the guardrail can distinguish PII the user *supplied* from PII the model *fabricated*.

Memory is **best-effort** — a Memory outage never fails an answer. Every `AskResponse` carries `citations`, the `used` leg counts (`{graph, keyword, rag}`), `trace_id`, and `duration_ms`. The copilot answers strictly over **this org's** context; if retrieval is empty it says so rather than guessing.

### Layer 3 — the MCP facet (`mcp-eng-memory`, for autonomous agents)

The strategic layer. A **stateless** Contract-4 MCP server registered as `mcp-eng-memory@1.0.0`, so AI coding agents (and xAgent) can query the engineering memory the same way a human uses the copilot. It exposes the same logic as Layer 1/2 — there is no second brain, only a second door.

`POST /mcp/v1/invoke` pipeline (`mcp-eng-memory/api/invoke.py`): auth (coarse `tool:invoke`) → fine scope `tool:mcp-eng-memory:invoke` → body-size cap → parse `{tool, args}` → input-schema validation (422 + JSON Pointer) → dispatch to the cypherx-a1 backend → output cap → cited result. The envelope is `{tool, output, citations, duration_ms, trace_id}`.

| MCP tool | Dispatches to | Kind |
|----------|---------------|------|
| `who_owns` | `POST /v1/graph/who-owns` | graph, no-LLM |
| `why_built` | `POST /v1/graph/why-built` | graph, no-LLM |
| `what_breaks_if_changed` | `POST /v1/graph/what-breaks` | graph, no-LLM |
| `experts_on` | `POST /v1/graph/experts` | graph, no-LLM |
| `graph_neighbors` | `POST /v1/graph/neighbors` | graph, no-LLM |
| `incident_root_cause` | `POST /v1/copilot/ask` | LLM, synthesized |
| `how_does_x_work` | `POST /v1/copilot/ask` | LLM, synthesized |

Three invariants make this layer safe:

1. **All tools are READ-ONLY and source-citing.** An agent can always re-verify by following the citation.
2. **The server is stateless and emits NO metering.** Per-invocation metering is the **caller's (xAgent's) outbox** (Contract-14) — never the tool's.
3. **Identity flows by token, never by body.** Contract-12 service token in `Authorization`, the agent forwarded via `X-Forwarded-Agent-JWT`, W3C `traceparent` propagated. Bodies carry no identity.

---

## 7. Where cypherx-a1 sits on the platform

cypherx-a1 is a **consuming app** — a peer of `xAgent/ax-1`, not a SharedCore service. It pushes no business logic into SharedCore; it consumes SharedCore strictly through versioned `/v1` contracts with additive-field tolerance.

| SharedCore service | cypherx-a1 uses it for | Hard boundary |
|--------------------|------------------------|---------------|
| **Auth** | Verify inbound agent JWT (JWKS), mint Contract-12 service token, register agents/keys | App owns `resource_acls` (per-repo/team). Auth never models repos/teams. |
| **llms-gateway** | The **only** path to a provider — extraction chat (`response_format=json_object`) + copilot answers | Embeddings reached *indirectly* via RAG. `Idempotency-Key` on calls; `llm_call_id` is the billing key, never rewritten. |
| **RAG** | The vector/semantic corpus; per-tenant KBs `eng-code` / `eng-conversations` / `eng-docs` / `eng-incidents` | Embedding model **explicitly pinned** (never the `embed` alias). The graph **never** enters RAG. Hybrid fusion/keyword/rerank stay app-side. |
| **Memory** | Copilot conversational working memory only (per-principal episodic) | The knowledge graph **must not** go here. |
| **Guardrails** | Pre/post copilot screening (`/v1/check/input`, `/v1/check/output` with `input_text`) | Fail-closed; `block` → `422 GUARDRAIL_VIOLATION`. |
| **Tool Registry + MCP** | Register `mcp-eng-memory@1.0.0`; agents invoke it | Metering is the **caller's** outbox, never the tool's. |

Runtime footprint: Postgres schema `cypherx_a1`, runtime role `cxa1_user` (non-superuser, no `BYPASSRLS`, cannot `CREATE EXTENSION`), `FORCE ROW LEVEL SECURITY` on every tenant-scoped table via `app.tenant_id` with a `NULLIF` guard (unset GUC → zero rows, never an error). The `outbox` table is the one exception — no RLS, because the cross-tenant publisher drains it without setting `app.tenant_id`; isolation lives in the payload. Events flow to Kafka topics `cypherx.cypherxa1.*` (Contract-5 envelope, `partition_key = tenant_id`, paired `.dlq`).

---

## 8. Design principles (the short list)

1. **Living memory, not dead documentation** — the system reads the sources; humans never re-author.
2. **Citations are the unit of truth** — every answer and every tool result is verifiable at the source; answers are never uncited.
3. **Graph for structure, RAG for semantics, Memory for conversation** — three planes, three jobs, no overlap; the graph never enters RAG or Memory.
4. **Consume SharedCore, don't reinvent it** — no second LLM path, no second vector store, no second auth; reuse via `/v1` contracts only.
5. **Tenant isolation is architectural, not procedural** — Postgres RLS + `FORCE` + non-superuser role make cross-tenant access impossible, not merely discouraged.
6. **Bitemporal by default** — history is retained; the memory can answer *as-of* questions.
7. **Connector-agnostic core** — `CanonicalRecord` makes every connector beyond GitHub additive, never a rewrite.
8. **Agents are first-class consumers** — the MCP facet is leverage, not an afterthought; the copilot is just the human's door to the same brain.

---

*See also: `01-architecture-and-request-flow.md`, `02-sharedcore-integration-boundary.md`, `03-data-model-and-schema.md`, `04-retrieval-and-copilot.md`, `05-mcp-eng-memory.md`.*
