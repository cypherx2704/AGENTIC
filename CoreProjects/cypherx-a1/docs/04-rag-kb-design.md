# RAG knowledge-base design

> cypherx-a1 leases a small, fixed set of per-tenant SharedCore RAG knowledge bases — `eng-code` / `eng-conversations` / `eng-docs` / `eng-incidents` — each pinned to an **explicit** embedding model at a fixed **dim 1536**, bound **create-once** and persisted immutably in `cypherx_a1.rag_kbs`. The graph never enters RAG; RAG holds only opaque text plus provenance metadata, and every chunk maps back to a graph entity through a `doc_id → entity_id` citation.

---

## 1. Where RAG sits in cypherx-a1

cypherx-a1 ("Autonomous Engineering Memory") is a **consuming app** (a peer of `xAgent/ax-1`), not a SharedCore service. It owns the knowledge **graph** (Postgres schema `cypherx_a1`, RLS by `tenant_id`) and leases SharedCore RAG strictly over the versioned `/v1` HTTP contract as its **dense-retrieval corpus**. The division of storage is hard and deliberate:

| Concern | Lives in | Owned by |
|---|---|---|
| Knowledge **graph** (entities, edges, identities) | Postgres `cypherx_a1` | this app |
| Raw landing, connectors, extraction ledger, **citations**, ACLs, KB bindings | Postgres `cypherx_a1` | this app |
| **Vectors / chunks** (opaque text + JSONB metadata) | SharedCore RAG service | RAG |
| Copilot conversational working memory | SharedCore Memory service | Memory |

The app stores only a **`vector_ref`** (a `{kb_id, doc_id}` pointer) on the graph node; the embeddings themselves never come back into `cypherx_a1`. This is restated at the top of the init migration (`db/migrations/20260614_0001__init.sql`):

```
--   * The GRAPH + raw landing + connectors + extraction ledger live HERE (app-owned).
--   * VECTORS live in the SharedCore RAG service (this app stores only a vector_ref).
--   * Copilot conversational memory lives in the SharedCore Memory service.
```

**Identity is header-only.** Every RAG call carries a Contract-12 service token in `Authorization: Bearer <service_jwt>`, the originating agent JWT forwarded as `X-Forwarded-Agent-JWT`, and W3C trace headers. Request bodies carry **no identity**. See `RagClient._headers()` in `src/cypherx_a1/services/rag_client.py`:

```python
headers = {
    "Authorization": f"Bearer {service_jwt}",
    "X-Forwarded-Agent-JWT": agent_jwt,
    **trace.propagation_headers(),
}
```

---

## 2. The four logical knowledge bases

cypherx-a1 leases exactly **four** logical KBs per tenant. The logical names are a closed `Literal` set in `src/cypherx_a1/models/canonical.py`:

```python
# Logical RAG KB names (resolved to a kb_id per tenant at first use).
KbName = Literal["eng-code", "eng-conversations", "eng-docs", "eng-incidents"]
```

| Logical KB | Holds | Example documents (GitHub-first MVP) | Originating node kind |
|---|---|---|---|
| `eng-code` | Code-change discourse: PR titles + bodies, diffs-as-prose, review threads | `PR owner/repo#123: <title>` | `pr` |
| `eng-conversations` | Chat / thread discussion (Slack-style, future connectors) | message threads | `person`, `pr`, `ticket` |
| `eng-docs` | Long-form engineering docs: issues, design docs, READMEs, ADRs | `Issue owner/repo#issue-45: <title>` | `ticket`, `document` |
| `eng-incidents` | Postmortems, incident timelines, outage narratives | incident reports | `incident`, `decision` |

The KB split is **not** a tenancy boundary (all four share one tenant) — it is a **corpus-shape** boundary so dense retrieval stays topically coherent and so each KB can be queried, refreshed, or ACL-gated independently. Per-repo / per-team authorization is enforced **app-side** via `cypherx_a1.resource_acls`, never by carving up KBs.

### 2.1 Which connector writes which KB

A connector's `to_canonical` emits zero or more `RagDoc`s, each tagged with its destination `kb` (`src/cypherx_a1/models/canonical.py`):

```python
@dataclass
class RagDoc:
    kb: KbName
    name: str
    content: str
    node: NodeRef
    metadata: dict[str, Any] = field(default_factory=dict)
    source_type: Literal["markdown", "text"] = "markdown"
```

The GitHub connector (`src/cypherx_a1/connectors/github.py`) routes:

- a **pull request** → `kb="eng-code"`, `content = f"# {title}\n\n{body}"`, linked to the `pr` node;
- an **issue** → `kb="eng-docs"`, `content = f"# {title}\n\n{body}"`, linked to the `ticket` node.

Each `RagDoc` is bound to exactly **one** graph node by `NodeRef`, which the pipeline resolves to an `entity_id` for the citation link.

---

## 3. Pinned embedding model + dim 1536

The single most load-bearing decision in this design: **the embedding model is pinned to an explicit literal, never the repointable `embed` alias.**

### 3.1 Why the alias is forbidden

SharedCore RAG exposes an `embed` alias that can be repointed at a newer embedding model over time. If cypherx-a1 created KBs against that alias, two KBs (or the same KB before/after a repoint) could end up in **different vector spaces** — silently breaking dense similarity, since vectors from model A and model B are not comparable. To guarantee one stable vector space for the lifetime of the corpus, cypherx-a1 passes the **explicit model name** as the alias at create time and records what RAG resolved it to.

### 3.2 The pinned defaults

From `src/cypherx_a1/core/config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `rag_embedding_model` | `text-embedding-3-small` | explicit model passed as `embedding_model_alias` on KB creation |
| `rag_embedding_dim` | `1536` | vector dimensionality; fallback when RAG omits `embedding_dim` |
| `rag_service_url` | `http://localhost:8087` | RAG `/v1` base (in-network `http://rag:8080`) |
| `rag_timeout_seconds` | `30.0` | client timeout |

`RagClient.create_kb()` sends the explicit model as the alias so RAG resolves it to a stable literal rather than the repointable default (`src/cypherx_a1/services/rag_client.py`):

```python
body = {
    "name": name,
    "description": f"cypherx-a1 engineering memory KB: {name}",
    "chunking_strategy": "sentence",
    "embedding_model_alias": self._settings.rag_embedding_model,
    "private": False,
}
```

The create response is captured as `KbInfo`, with the resolved model and dim defaulting to the pinned settings when RAG does not echo them:

```python
return KbInfo(
    kb_id=str(data["kb_id"]),
    embedding_model_resolved=str(data.get("embedding_model_resolved", "")),
    embedding_dim=int(data.get("embedding_dim", self._settings.rag_embedding_dim)),
)
```

> **Invariant.** Once a KB exists, its embedding model and dim are **immutable**. A future model change is a **new KB** (e.g. a `eng-code-v2` logical name and a re-ingest), never a repoint of the existing binding. The `rag_kbs` upsert is `ON CONFLICT … DO NOTHING` specifically to make the first writer's binding permanent (§4.2).

---

## 4. The `KbResolver`: create-once + persist binding

The `KbResolver` (`src/cypherx_a1/ingestion/pipeline.py`) maps a **logical** KB name to a concrete RAG `kb_id` **per tenant**, creating the KB exactly once and persisting the binding immutably. Its docstring states the contract:

```python
class KbResolver:
    """Resolves a logical KB name to a RAG ``kb_id`` per tenant (create-once, then cached).

    The resolved embedding model + dim are persisted immutably in ``cypherx_a1.rag_kbs`` so
    every KB shares one stable vector space; an in-process cache avoids a DB hit per doc."""
```

### 4.1 Resolution order (three tiers)

`KbResolver.resolve(pool, *, tenant_id, logical, agent_jwt, on_behalf_of)` walks three tiers, cheapest first:

| Tier | Check | Action |
|---|---|---|
| 1. In-process cache | `(tenant_id, logical)` in `self._cache` | return cached `kb_id` immediately (no DB, no HTTP) |
| 2. DB binding | `ingest_repo.get_rag_kb(conn, logical_name=logical)` inside `in_tenant` | cache + return the persisted `kb_id` |
| 3. Create | `RagClient.create_kb(name=f"cypherx-a1::{logical}", …)` | persist binding, re-read winner, cache + return |

The cache key is the `(tenant_id, logical)` tuple, so each tenant resolves its own `kb_id` even though all share the four logical names:

```python
self._cache: dict[tuple[str, str], str] = {}  # (tenant_id, logical) -> kb_id
```

The RAG-side KB **name** is namespaced to avoid colliding with other consuming apps in the tenant:

```python
kb_name = f"cypherx-a1::{logical}"   # e.g. "cypherx-a1::eng-code"
```

### 4.2 Persist-and-re-read (race-safe binding)

After creating the KB, the resolver persists the binding and **re-reads the winner** in the same `in_tenant` tx, so concurrent ingest workers all converge on a single `kb_id`:

```python
async def _persist(conn: AsyncConnection) -> dict | None:
    await ingest_repo.set_rag_kb(
        conn,
        logical_name=logical,
        kb_id=info.kb_id,
        model=info.embedding_model_resolved or self._settings.rag_embedding_model,
        dim=info.embedding_dim,
    )
    return await ingest_repo.get_rag_kb(conn, logical_name=logical)

winner = await in_tenant(pool, tenant_id, _persist)
kb_id = winner["kb_id"] if winner else info.kb_id
self._cache[key] = kb_id
```

The persist is **idempotent and first-writer-wins** — `set_rag_kb` is `ON CONFLICT (tenant_id, logical_name) DO NOTHING` (`src/cypherx_a1/db/ingest_repo.py`):

```sql
INSERT INTO cypherx_a1.rag_kbs (tenant_id, logical_name, kb_id, embedding_model_resolved, embedding_dim)
VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s)
ON CONFLICT (tenant_id, logical_name) DO NOTHING
```

If two workers race and each creates a RAG KB, the first to commit wins the row; the loser's create still returns a `kb_id`, but the **re-read** returns the persisted winner, so both processes (and the cache) end up bound to the same KB. The model passed into the binding prefers RAG's `embedding_model_resolved`, falling back to the pinned `rag_embedding_model` if RAG returns an empty string.

### 4.3 The `rag_kbs` binding table

The binding lives in `cypherx_a1.rag_kbs` (`db/migrations/20260614_0001__init.sql`), RLS-scoped by `tenant_id`, primary-keyed on `(tenant_id, logical_name)`:

| Column | Type | Notes |
|---|---|---|
| `tenant_id` | `UUID NOT NULL` | RLS scope; part of PK |
| `logical_name` | `VARCHAR(60) NOT NULL` | `eng-code` \| `eng-conversations` \| `eng-docs` \| `eng-incidents` |
| `kb_id` | `TEXT NOT NULL` | the RAG-assigned KB id (opaque) |
| `embedding_model_resolved` | `TEXT NOT NULL` | the literal model RAG pinned at creation |
| `embedding_dim` | `INTEGER NOT NULL` | vector dim (1536) |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | |

```sql
PRIMARY KEY (tenant_id, logical_name)
```

`rag_kbs` is in the RLS array (`ENABLE`/`FORCE ROW LEVEL SECURITY`, isolation policy `USING app.tenant_id`) and the runtime role `cxa1_user` is granted `SELECT, INSERT, UPDATE, DELETE` on it. No `UPDATE` path is exercised by the resolver — the binding is write-once in practice.

---

## 5. Document shape and `chunk.metadata` schema

### 5.1 What gets ingested

A `RagDoc` is inline-ingested by `RagClient.ingest_inline()` to `POST /v1/kbs/{kb_id}/documents`. The wire body is small and deliberately opaque:

```python
body = {
    "name": name[:500],
    "content": content,
    "source_type": source_type if source_type in ("markdown", "text") else "text",
    "metadata": metadata,
}
```

- `name` is truncated to 500 chars.
- `source_type` is forced into `{"markdown", "text"}` (anything else degrades to `"text"`).
- `content` is the opaque text RAG chunks + embeds. **No graph structure is ever placed in `content`.**

### 5.2 The provenance metadata

The pipeline enriches the connector-supplied `doc.metadata` with two app-controlled keys before ingest (`_ingest_one` in `src/cypherx_a1/ingestion/pipeline.py`):

```python
metadata = {**doc.metadata, "node_id": entity_id, "kb": doc.kb}
```

This produces the chunk-metadata schema RAG persists on every chunk. The orchestrator later reads `chunk.metadata` to map a hit back to its graph entity for citation reinforcement.

| Metadata key | Source | Purpose | Example |
|---|---|---|---|
| `node_id` | pipeline (`entity_id`) | the originating graph entity UUID — the spine of the citation | `4f3c…-uuid` |
| `kb` | pipeline (`doc.kb`) | the logical KB the doc belongs to | `eng-code` |
| `repo` | connector | repository `owner/name` | `acme/payments` |
| `pr_number` | connector (PR docs) | PR number within the repo | `123` |
| `author` | connector (PR docs) | GitHub login of the PR author | `octocat` |
| `file_path` | connector (code-scoped docs) | path within the repo (when present) | `src/auth/token.py` |
| `url` | connector | canonical HTML URL of the source | `https://github.com/...` |
| `issue_number` | connector (issue docs) | issue number within the repo | `45` |

> **`@>`-containment compatibility.** Because RAG only supports JSONB `@>`-containment filters (no range / comparison operators), every metadata value is stored as an exact-match scalar (string / ISO-8601 string / number). There are **no** server-side range fields; any time-window or numeric-range narrowing happens **app-side** after the hits return (§6.3).

A representative GitHub PR doc (from `_pr_record` in `connectors/github.py`):

```python
RagDoc(
    kb="eng-code",
    name=f"PR {pr_key}: {title}",
    content=f"# {title}\n\n{body}",
    node=pr.ref,
    metadata={
        "repo": full_name,
        "pr_number": number,
        "author": author.attrs.get("login"),
        "url": html_url,
    },
)
```

After the pipeline merge, the chunk's persisted metadata is:

```json
{
  "repo": "acme/payments",
  "pr_number": 123,
  "author": "octocat",
  "url": "https://github.com/acme/payments/pull/123",
  "node_id": "4f3c…-uuid",
  "kb": "eng-code"
}
```

---

## 6. Retrieval constraints (`top_k` / `ef_search` / filters)

cypherx-a1 consumes RAG as **dense-only** in the first cycle. Hybrid fusion, keyword search, reranking, and query expansion are all **app-side** (the orchestrator runs graph + keyword legs in Postgres and fuses with RRF). RAG sees a single dense query per KB.

### 6.1 The query call

`RagClient.query()` posts to `POST /v1/kbs/{kb_id}/query` with a clamped body (`src/cypherx_a1/services/rag_client.py`):

```python
body: dict[str, Any] = {
    "query": query,
    "top_k": min(top_k, 100),
    "min_score": self._settings.rag_query_min_score,
    "search_mode": "dense",
    "ef_search": min(self._settings.rag_query_ef_search, 500),
}
if filters:
    body["filters"] = filters
```

| Parameter | Default (`config.py`) | Hard ceiling enforced in client | Notes |
|---|---|---|---|
| `top_k` | `rag_query_top_k = 20` | `min(top_k, 100)` | max 100 results per KB |
| `ef_search` | `rag_query_ef_search = 100` | `min(…, 500)` | HNSW search-width ceiling 500 |
| `min_score` | `rag_query_min_score = 0.0` | — | server-side floor |
| `search_mode` | — | `"dense"` (fixed) | dense-only first cycle |
| `filters` | — | `@>`-containment only | omitted when empty |

The clamps are enforced **in the client**, not trusted to the caller — a caller asking for `top_k=10000` is silently capped at 100, and `ef_search` at 500.

### 6.2 Fan-out across KBs

The `RetrievalOrchestrator` (`src/cypherx_a1/retrieval/orchestrator.py`) resolves the tenant's KB ids and queries **each KB** for dense hits, then fuses RAG hits with the graph and keyword legs via Reciprocal Rank Fusion:

```python
for kb_id in kb_ids:
    res = await self._rag.query(
        kb_id=kb_id, query=question, top_k=top_k, agent_jwt=agent_jwt, on_behalf_of=agent_id
    )
    if res.forbidden:
        continue
    for h in res.results:
        rag_hits.append({...})
```

A `403` KB-ACL deny is returned as `forbidden=True` (**not** raised), so the orchestrator degrades gracefully and simply skips that KB rather than failing the whole retrieval:

```python
if resp.status_code == 403:
    metrics.downstream_calls_total.labels("rag", "forbidden").inc()
    return RagQueryResult(kb_id=kb_id, results=[], forbidden=True)
```

Any other non-2xx (including transport errors) raises `ApiError(SERVICE_UNAVAILABLE)`.

### 6.3 Filter discipline: `@>`-containment only

RAG's `filters` map supports **only JSONB `@>`-containment** — exact-match on the metadata document. The two consequences for cypherx-a1:

1. **Filter by exact metadata** (e.g. `{"repo": "acme/payments"}` or `{"kb": "eng-incidents"}`) is pushed down to RAG.
2. **Range / time-window filters are app-side.** Any field that would need `<`, `>`, or a window (timestamps, numeric ranges) is stored as an **ISO-8601 string** in metadata and the narrowing is applied **after** the hits return, in app code. RAG never sees a range predicate.

This keeps the app forward-compatible with additive RAG fields (additive-field tolerance) while never depending on a filter capability RAG does not ship in the first cycle.

---

## 7. Ingestion modes

The pipeline (`src/cypherx_a1/ingestion/pipeline.py`) has two distinct ingestion modes governed by whether an **inbound agent JWT** is available to forward to RAG.

| Mode | Trigger | Lands raw event | Normalizes graph | Embeds into RAG | Citation + event |
|---|---|---|---|---|---|
| **Full (authenticated sync / backfill)** | `rag`, `kb_resolver`, and `agent_jwt` all present | ✅ | ✅ | ✅ inline | ✅ |
| **Deferred (webhook path)** | `rag`/`kb_resolver` `None` **or** no `agent_jwt` | ✅ | ✅ | ⏸️ skipped | ⏸️ deferred |

`ingest_records()` documents the deferred path explicitly:

```python
"""...When ``rag``/``agent_jwt`` are None (e.g. the webhook path, which has
no inbound agent JWT to forward) the docs are NOT embedded — only landing + graph
normalization run, and RAG enrichment is deferred to an authenticated sync / worker."""
```

And `_ingest_one()` short-circuits the RAG leg accordingly:

```python
# 3) RAG ingest each doc (HTTP, outside any tx) ...
# Skipped on the webhook path (no agent JWT to forward) — RAG enrichment is deferred.
if rag is None or kb_resolver is None or not agent_jwt:
    return
```

This is a security stance, not an oversight: a webhook receiver has no agent identity to forward, so embedding (which RAG meters and ACL-checks against the forwarded agent JWT) is postponed to a later **authenticated** sync that re-walks unembedded nodes. The graph stays current immediately; vectors catch up under a real identity.

### 7.1 Inline-only, ≤100 KiB

All embedding is **inline** (`RagClient.ingest_inline()` → `POST /v1/kbs/{kb_id}/documents`); there is no presigned / Kafka-worker ingest path in cypherx-a1's first cycle. The document must be **≤100 KiB** (the RAG inline ceiling). Larger source bodies are the connector's responsibility to summarize / split before emitting a `RagDoc`.

### 7.2 The full-mode pipeline, step by step

For each `CanonicalRecord`, `_ingest_one` runs:

1. **Land + normalize in one tenant tx.** `ingest_repo.record_raw_event(...)` lands the raw event idempotently; if it is a duplicate, the record short-circuits (no re-processing, no re-embed). Otherwise `upsert_graph(conn, record)` upserts nodes + edges and returns the `NodeRef → entity_id` map.
2. **Per-doc RAG ingest (HTTP, outside any tx).** For each `RagDoc`, resolve the `kb_id` via `KbResolver.resolve(...)`, merge `{node_id, kb}` into metadata, and `rag.ingest_inline(...)`.
3. **Link + event (a second tenant tx).** Record the `vector_ref` on the node, insert the citation, and enqueue the outbox event — see §8 and §9.

### 7.3 Idempotency at three layers

| Layer | Mechanism | Effect |
|---|---|---|
| Raw landing | `raw_events` unique `(tenant_id, source, external_id, content_sha)`, `ON CONFLICT DO NOTHING` | a re-seen record never re-processes or re-embeds |
| RAG ingest | `Idempotency-Key: f"{tenant_id}:{record.content_sha}:{doc.kb}"` | a retried HTTP ingest does not create a duplicate doc / double-bill embeddings |
| Graph node identity | partial unique index on the current slice (`valid_to IS NULL`) | re-ingest updates the node in place and keeps the same `entity_id` |

The RAG idempotency key (`pipeline.py`):

```python
idempotency_key=f"{tenant_id}:{record.content_sha}:{doc.kb}",
```

Because the key is keyed on `content_sha`, a **content change** produces a new key (a new embed); an unchanged re-sync reuses the key (no re-embed, no re-spend).

---

## 8. Delete-and-re-ingest refresh

cypherx-a1 keeps the corpus fresh with a **delete-and-re-ingest** model, not in-place mutation of RAG chunks. The reasons are structural:

- RAG chunks are **opaque** — the app cannot edit a chunk's text in place.
- Re-chunking after a content edit can change chunk boundaries and chunk ids, so partial updates would orphan citations.

The refresh contract:

| Step | What happens | Where |
|---|---|---|
| 1. Detect change | a new `content_sha` for the same `(source, external_id)` lands a **new** raw event (the old `content_sha` row stays) | `raw_events` (idempotent landing) |
| 2. Drop stale citations | the app deletes the prior `doc_id`'s citation rows (the runtime role is granted `DELETE` on `citations`) | `cypherx_a1.citations` |
| 3. Re-ingest | a fresh `rag.ingest_inline` under a **new** idempotency key (new `content_sha`) creates a new `doc_id` / chunks | RAG `POST /v1/kbs/{kb_id}/documents` |
| 4. Rebind | `graph_repo.set_vector_ref` overwrites the node's `vector_ref` with the new `{kb_id, doc_id}`; a fresh citation is inserted | `entities.vector_ref`, `citations` |

The grants confirm the delete-and-re-insert (never update) shape on citations (`db/migrations/20260614_0001__init.sql`):

```sql
GRANT SELECT, INSERT, DELETE ON cypherx_a1.citations TO cxa1_user;
```

> **No re-spend on unchanged content.** Because the embedding ingest is gated by raw-event landing **and** an idempotency key derived from `content_sha`, an unchanged re-sync neither lands a new raw event nor re-embeds. Refresh cost is paid **only** when content actually changes. Extraction spend is likewise guarded — `extraction_jobs` is an idempotency + cost ledger keyed on `(tenant_id, node_id, content_sha, extractor_version)`, so "re-ingest never re-spends" (`extraction/extractor.py`).

The graph node keeps a **stable** `entity_id` across refresh (the partial unique index on `valid_to IS NULL`), so citations from older answers still resolve to the same entity even after the underlying `doc_id` is rotated.

---

## 9. `doc_id → entity` citation linkage

The whole point of the metadata + binding machinery is to make every retrieved chunk **citable** back to a concrete graph entity. The link is recorded **at ingest time** and resolved **at retrieval time**.

### 9.1 Recording the link at ingest

After a successful `ingest_inline`, the pipeline records the `vector_ref` and the citation in one tenant tx, then enqueues the outbox event (`_link` in `pipeline.py`):

```python
async def _link(conn, _kb_id=kb_id, _doc_id=ingested.doc_id, _eid=entity_id):
    await graph_repo.set_vector_ref(conn, entity_id=_eid, vector_ref={"kb_id": _kb_id, "doc_id": _doc_id})
    await ingest_repo.add_citation(conn, kb_id=_kb_id, doc_id=_doc_id, chunk_id=None, entity_id=_eid)
    await enqueue_event(conn, topic=TOPIC_RECORD_NORMALIZED, ...)
```

Note `chunk_id=None`: at ingest the chunk ids are not yet known (RAG chunks asynchronously), so **`doc_id` is the stable citation key**. There is exactly **one** `RagDoc` per graph node per ingest, so the `doc_id → entity_id` mapping is 1:1 and durable. `chunk_id` is filled opportunistically later for finer-grained reinforcement; it is never required.

### 9.2 The `citations` table

`cypherx_a1.citations` (`db/migrations/20260614_0001__init.sql`) stores the provenance link, RLS-scoped by `tenant_id`:

| Column | Type | Notes |
|---|---|---|
| `citation_id` | `UUID PK DEFAULT gen_random_uuid()` | |
| `tenant_id` | `UUID NOT NULL` | RLS scope |
| `kb_id` | `TEXT NOT NULL` | which KB the doc lives in |
| `doc_id` | `TEXT` | **the stable citation key** |
| `chunk_id` | `TEXT` | optional finer-grained link |
| `entity_id` | `UUID` | the originating graph entity |
| `edge_id` | `UUID` | optional — extraction can cite an *edge* (e.g. a `depends_on` decision) |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` | |

Indexes:

```sql
CREATE INDEX idx_citations_tenant ON cypherx_a1.citations (tenant_id);
CREATE INDEX idx_citations_chunk  ON cypherx_a1.citations (tenant_id, chunk_id);
```

### 9.3 Resolving the link at retrieval

After the RAG fan-out, the orchestrator collects the hit `doc_id`s and maps them back to current entities via `ingest_repo.entities_for_docs` (`orchestrator.py`):

```python
doc_ids = [h["doc_id"] for h in rag_hits if h.get("doc_id")]
async def _docmap(conn):
    return await ingest_repo.entities_for_docs(conn, doc_ids=doc_ids)
doc_entity = await in_tenant(pool, tenant_id, _docmap) if doc_ids else {}
```

`entities_for_docs` joins citations to the **current** entity slice and returns one entity per `doc_id` (`src/cypherx_a1/db/ingest_repo.py`):

```sql
SELECT DISTINCT ON (c.doc_id) c.doc_id, e.entity_id, e.kind, e.natural_key, e.title
  FROM cypherx_a1.citations c
  JOIN cypherx_a1.entities e ON e.entity_id = c.entity_id AND e.valid_to IS NULL
 WHERE c.doc_id = ANY(%s)
```

A parallel `entities_for_chunks` exists for chunk-level back-mapping, with the same join shape keyed on `c.chunk_id`. The `DISTINCT ON (c.doc_id)` plus the `valid_to IS NULL` join is what makes the citation survive a delete-and-re-ingest refresh: even if multiple citation rows point at the same entity across refreshes, the resolver returns one **current** entity per `doc_id`.

### 9.4 Citation reinforcement in fusion

Resolved entities feed Reciprocal Rank Fusion: a RAG hit whose `doc_id` maps to an entity reinforces that entity's score in the fused ranking (`orchestrator.py`), so dense evidence and graph evidence about the **same** entity compound rather than competing. The dense leg therefore never produces an uncitable answer — every chunk either resolves to a graph entity (and is cited) or is dropped from the entity-level fusion.

---

## 10. End-to-end flow (full mode)

```
connector.to_canonical(record)
        │  RagDoc{kb, name, content, node, metadata}
        ▼
ingest_records → _ingest_one
        │
        ├─[tx1: in_tenant] record_raw_event (idempotent)  ──duplicate?──▶ skip
        │                  upsert_graph → {NodeRef: entity_id}
        │
        ├─ KbResolver.resolve(tenant_id, logical=doc.kb)
        │        cache ▸ rag_kbs ▸ create_kb("cypherx-a1::<logical>")  [model pinned, dim 1536]
        │
        ├─ metadata = {**doc.metadata, node_id: entity_id, kb: doc.kb}
        ├─ rag.ingest_inline(kb_id, content, metadata, Idempotency-Key=tenant:sha:kb) → doc_id
        │
        └─[tx2: in_tenant] set_vector_ref(entity_id, {kb_id, doc_id})
                           add_citation(kb_id, doc_id, chunk_id=None, entity_id)
                           enqueue_event(cypherx.cypherxa1.record.normalized)
```

At query time:

```
question
   ├─ graph leg (Postgres find_entities)        ┐
   ├─ keyword leg (Postgres keyword_search)     ├─ RRF fusion (app-side)
   └─ RAG dense leg (per kb_id, top_k≤100,      ┘
        ef_search≤500, @>-filters)
            └─ doc_id → entities_for_docs → entity citations
```

---

## 11. Design invariants (do-not-break list)

| # | Invariant | Enforced by |
|---|---|---|
| 1 | The **graph never enters RAG**; `content` is opaque text + provenance only | connectors / pipeline (no graph in `content`) |
| 2 | Embedding model is **explicit + pinned**, never the `embed` alias | `RagClient.create_kb` sends `embedding_model_alias=rag_embedding_model` |
| 3 | Embedding **dim is 1536**, fixed for the corpus lifetime | `rag_embedding_dim`; persisted in `rag_kbs.embedding_dim` |
| 4 | KB binding is **create-once + immutable**, race-safe | `KbResolver` + `rag_kbs` PK `(tenant, logical)` + `ON CONFLICT DO NOTHING` |
| 5 | Inline ingest only, **≤100 KiB** | `RagClient.ingest_inline` |
| 6 | Query clamps: `top_k ≤ 100`, `ef_search ≤ 500`, **`@>`-filters only**, `search_mode="dense"` | `RagClient.query` |
| 7 | Range/time filters are **app-side** (ISO strings in metadata) | orchestrator / metadata schema |
| 8 | **Every chunk is citable**: `doc_id → entity_id` recorded at ingest | `citations` + `entities_for_docs` |
| 9 | Refresh is **delete-and-re-ingest**, never in-place chunk mutation | `citations` DELETE grant + idempotency key on `content_sha` |
| 10 | **No re-spend** on unchanged content (embed + extraction both ledgered) | `raw_events` landing, RAG `Idempotency-Key`, `extraction_jobs` |
| 11 | Identity is **header-only** (Contract-12 token + forwarded agent JWT + trace); bodies carry no identity | `RagClient._headers` |
| 12 | Webhook path is **graph-only**; RAG enrichment deferred to an authenticated sync | `_ingest_one` guard on `agent_jwt` |
| 13 | A `403` KB-ACL deny **degrades** (skip KB), it does not fail retrieval | `RagClient.query` → `forbidden=True` |

---

## 12. Reference: file map

| Concern | File |
|---|---|
| Ingestion pipeline + `KbResolver` | `src/cypherx_a1/ingestion/pipeline.py` |
| Canonical model (`RagDoc`, `KbName`, `NodeRef`) | `src/cypherx_a1/models/canonical.py` |
| RAG `/v1` client (create / ingest / query) | `src/cypherx_a1/services/rag_client.py` |
| KB-binding + citation data access | `src/cypherx_a1/db/ingest_repo.py` |
| `vector_ref` writes | `src/cypherx_a1/db/graph_repo.py` |
| Hybrid retrieval + RRF fusion | `src/cypherx_a1/retrieval/orchestrator.py` |
| GitHub connector (`RagDoc` emission) | `src/cypherx_a1/connectors/github.py` |
| Pinned RAG settings | `src/cypherx_a1/core/config.py` |
| `rag_kbs` / `citations` schema + RLS + grants | `db/migrations/20260614_0001__init.sql` |
