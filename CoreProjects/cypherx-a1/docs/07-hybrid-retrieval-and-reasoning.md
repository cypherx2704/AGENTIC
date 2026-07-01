# Hybrid retrieval & reasoning

> How cypherx-a1 fuses a graph leg, a RAG-dense leg, and a tsvector keyword leg with reciprocal-rank fusion, maps RAG chunks back to graph entities via `doc_id`, builds a token-bounded cited context for the copilot, and answers the core engineering-memory questions (`who_owns` / `what_breaks` / `experts_on`) with recursive-CTE graph algorithms — all app-side, all RLS-scoped.

---

## 1. Why retrieval is the crown jewel's workload

cypherx-a1 turns scattered engineering history into a queryable memory by holding two complementary stores:

- the **app-owned knowledge graph** (`cypherx_a1.entities` + `cypherx_a1.edges`) — typed, bitemporal, deterministic, the thing that knows *structure* (who owns what, what depends on what, who decided what); and
- the **leased RAG corpus** (per-tenant KBs in the SharedCore RAG service) — opaque text chunks + JSONB metadata, the thing that knows *semantics* (which prose is about your question).

Neither alone is enough. The graph is precise but only as rich as its keywords; dense vectors are fuzzy but blind to ownership/impact structure. **Hybrid retrieval is the bridge**, and per the locked SharedCore boundary it is owned *entirely* by cypherx-a1: **RAG ships dense-only first cycle**, so keyword (tsvector), reciprocal-rank fusion (RRF), the citation back-map, query-time range filtering, and rerank all live here. The GRAPH never enters RAG and never enters Memory.

This document is the authoritative description of two surfaces:

| Surface | Code | Used by |
| --- | --- | --- |
| **Hybrid retrieval** — fuse 3 legs into a cited, token-bounded context | `src/cypherx_a1/retrieval/orchestrator.py` | the copilot (`POST /v1/copilot/ask`) |
| **Graph reasoning** — deterministic structured answers, no LLM | `src/cypherx_a1/copilot/queries.py` + `src/cypherx_a1/db/graph_repo.py` | `POST /v1/graph/*` REST + `mcp-eng-memory` MCP tools |

Both run read-only inside an `in_tenant` transaction, so RLS scopes every read to the caller's tenant.

---

## 2. The RetrievalOrchestrator at a glance

`RetrievalOrchestrator.retrieve()` (in `src/cypherx_a1/retrieval/orchestrator.py`) is the single entrypoint. The copilot calls it as step 3 of its flow (`CopilotService.ask`, `copilot/service.py`), after PRE-guardrail screening and before prompt build.

```
async def retrieve(
    self, pool, *, tenant_id, agent_jwt, agent_id, question, top_k,
) -> RetrievalResult
```

It runs **three independent legs**, fuses them with **RRF**, maps RAG hits back to graph entities for citation reinforcement, and returns a `RetrievalResult` carrying ranked `EvidenceItem`s plus a `used` leg-count summary.

```
                         question
                            │
        ┌───────────────────┼───────────────────────────┐
        │ (one tenant tx)   │                            │ (HTTP, no tx)
        ▼                   ▼                            ▼
   LEG 1: graph        LEG 3: keyword              LEG 2: rag-dense
   graph_repo          graph_repo                  RagClient.query
   .find_entities      .keyword_search             per resolved KB id
   (FTS + natural_key) (tsvector ts_rank)          (search_mode="dense")
        │                   │                            │
        │                   │                  doc_id ──► ingest_repo
        │                   │                            .entities_for_docs
        │                   │                            (doc_id → entity)
        └─────────┬─────────┴──────────────┬─────────────┘
                  ▼                         ▼
            ════════════  RRF fusion (1/(k+rank)) ════════════
                  │
                  ▼
        sorted by rrf_score, sliced to retrieval_context_max_chunks
                  │
                  ▼
        RetrievalResult { items[], used{} }
          ├─ .context_text(max_chars)  → token-bounded prompt context
          └─ .citations()             → list[Citation], every answer cited
```

### 2.1 The three legs

| # | Leg | Function | What it ranks | Limit setting (default) |
| --- | --- | --- | --- | --- |
| 1 | **graph** | `graph_repo.find_entities(conn, query=question, limit=…)` | current entities by `ts_rank(fts, …)` **plus** an exact `natural_key` match | `retrieval_graph_limit` (20) |
| 2 | **rag-dense** | `RagClient.query(kb_id=…, query=question, top_k=…, search_mode="dense")` per KB | dense vector similarity (`score`) | `top_k` from the request (≤ 50; RAG caps at 100) |
| 3 | **keyword** | `graph_repo.keyword_search(conn, query=question, limit=…)` | current entities by `ts_rank(fts, …)` (the BM25-ish leg) | `retrieval_keyword_limit` (20) |

Legs 1 and 3 share **one** `in_tenant` transaction (along with resolving the KB ids); leg 2 is a sequence of HTTP calls outside any transaction. This ordering matters: the dense leg must not hold a DB connection while waiting on the network.

### 2.2 Why two FTS legs?

Leg 1 (`find_entities`) and leg 3 (`keyword_search`) both hit the `fts` tsvector but serve different roles:

- **`find_entities`** is the *graph entry-point*: it additionally matches an **exact `natural_key`** (so `who_owns("owner/repo")` resolves the repo node directly) and orders `(natural_key = q) DESC, rank DESC`. It returns `attrs` + `vector_ref` for citation building.
- **`keyword_search`** is the pure *lexical* leg: `fts @@ plainto_tsquery` ranked by `ts_rank` only, returning `search_text` + `vector_ref`. It exists because RAG is dense-only first cycle, so cypherx-a1 must own a keyword channel to recover exact-term matches that dense embeddings miss (identifiers, error codes, function names).

Feeding both into RRF means an entity that surfaces in *both* the structured and lexical channels gets a higher fused score — that reinforcement is the whole point.

---

## 3. The three legs in detail

### 3.1 Leg 1 — graph (FTS + natural-key)

`graph_repo.find_entities` (`db/graph_repo.py`):

```sql
SELECT entity_id, kind, source, natural_key, title, attrs, vector_ref,
       ts_rank(fts, plainto_tsquery('english', %(q)s)) AS rank
  FROM cypherx_a1.entities
 WHERE valid_to IS NULL
   AND (fts @@ plainto_tsquery('english', %(q)s) OR natural_key = %(q)s)
 ORDER BY (natural_key = %(q)s) DESC, rank DESC
 LIMIT %(limit)s
```

Notes load-bearing for accuracy:

- **`fts`** is a generated `tsvector` column on `cypherx_a1.entities` (see `docs/03-data-model-and-schema.md`). It is built from title/search_text at write time, so search is a plain GIN-indexed match with no runtime tsvector cost.
- **`valid_to IS NULL`** restricts to the *current* bitemporal slice. Superseded entity versions are never retrieved.
- The `OR natural_key = %(q)s` branch is what makes the orchestrator double as an entity resolver — a query that *is* a repo/service identifier pins that node deterministically.
- An optional `kinds` filter (`AND kind = ANY(%(kinds)s)`) exists; the orchestrator does not pass it (it wants all kinds), but the graph query tools do.

### 3.2 Leg 3 — keyword (tsvector)

`graph_repo.keyword_search`:

```sql
SELECT entity_id, kind, natural_key, title, search_text, vector_ref,
       ts_rank(fts, plainto_tsquery('english', %(q)s)) AS rank
  FROM cypherx_a1.entities
 WHERE valid_to IS NULL AND fts @@ plainto_tsquery('english', %(q)s)
 ORDER BY rank DESC
 LIMIT %(limit)s
```

`vector_ref` is selected here too, anticipating a future direct chunk back-map; the current fusion path maps RAG hits via `doc_id` (§5).

### 3.3 Leg 2 — rag-dense

The orchestrator first resolves the KB ids in the same tenant transaction as legs 1 and 3:

```python
async def _list_kb_ids(conn) -> list[str]:
    cur = await conn.execute("SELECT kb_id FROM cypherx_a1.rag_kbs")
    return [r[0] for r in await cur.fetchall()]
```

Then, **outside** any transaction, it queries each KB over HTTP via `RagClient.query` (`services/rag_client.py`):

```python
for kb_id in kb_ids:
    res = await self._rag.query(
        kb_id=kb_id, query=question, top_k=top_k,
        agent_jwt=agent_jwt, on_behalf_of=agent_id,
    )
    if res.forbidden:
        continue
    for h in res.results:
        rag_hits.append({"chunk_id": h.chunk_id, "doc_id": h.doc_id,
                         "content": h.content, "score": h.score,
                         "source_name": h.source_name, "source_uri": h.source_uri,
                         "metadata": h.metadata})
```

The request body `RagClient.query` sends to `POST /v1/kbs/{kb_id}/query` is constrained to honour the RAG `/v1` contract clamps:

| Field | Value | Why |
| --- | --- | --- |
| `query` | the question | — |
| `top_k` | `min(top_k, 100)` | RAG hard-caps `top_k` at 100 |
| `min_score` | `rag_query_min_score` (default `0.0`) | app-side floor |
| `search_mode` | `"dense"` | **RAG ships dense-only first cycle**; hybrid/keyword/rerank stay app-side |
| `ef_search` | `min(rag_query_ef_search, 500)` (default 100) | RAG caps `ef_search` at 500 |
| `filters` | optional `@>`-containment only | the orchestrator passes none; range/time filtering is app-side |

**Graceful degradation:** a `403` from RAG (a KB-ACL deny) is returned as `RagQueryResult(forbidden=True)` and *skipped*, never raised — the orchestrator degrades to whatever KBs the principal may read. Any other non-2xx (or transport error) raises `SERVICE_UNAVAILABLE`. Identity travels in HEADERS only: Contract-12 service token in `Authorization`, the forwarded agent JWT in `X-Forwarded-Agent-JWT`, plus W3C trace headers. **Bodies carry no identity.**

Each parsed hit is a `RagHit` with `chunk_id`, `doc_id`, `content`, `score`, `metadata`, `source_name`, `source_uri` — `source_name`/`source_uri` come from the response's nested `source` object (`item["source"]["name"|"uri"]`).

---

## 4. Reciprocal-rank fusion (the math)

RRF is the fusion rule that lets three legs with *incomparable* score scales (a `ts_rank` float, a dense cosine score, another `ts_rank`) vote on one ranking using only **position**, not magnitude.

### 4.1 The formula

For an item appearing at zero-based `rank` in a leg's result list, that leg contributes:

```
contribution = 1 / (k + rank)
```

where **`k = retrieval_rrf_k`** (default **60**, the canonical RRF constant). An item's fused score is the **sum** of its contributions across every leg it appears in:

```
RRF(item) = Σ_legs  1 / (k + rank_leg(item))
```

In code (`orchestrator.py`):

```python
k = s.retrieval_rrf_k

def _bump(key: str, rank: int) -> None:
    scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
```

`_bump` is called once per leg-appearance, with `rank` taken from `enumerate(...)` over each leg's ordered hits (graph, then keyword, then rag). After all three legs, the accumulated `scores[key]` is written onto each item's `rrf_score`, and items are sorted descending.

### 4.2 Worked example (`k = 60`)

| Item | graph rank | keyword rank | rag rank | RRF score |
| --- | --- | --- | --- | --- |
| Entity A | 0 | 2 | — | 1/60 + 1/62 = 0.01667 + 0.01613 = **0.03280** |
| Entity B | — | 0 | 1 (via doc_id) | 1/60 + 1/61 = 0.01667 + 0.01639 = **0.03306** |
| Entity C | 1 | — | — | 1/61 = **0.01639** |
| Chunk D (no entity) | — | — | 0 | 1/60 = **0.01667** |

Entity B edges out Entity A even though A is rank-0 in graph, because B is rank-0 in keyword *and* reinforced by a dense chunk — multi-leg agreement wins. This is precisely the "a chunk and its entity reinforce each other" property called out in the orchestrator's docstring.

### 4.3 Why `k = 60`

The constant dampens the gap between top ranks. With `k = 60`, rank-0 (`1/60 ≈ 0.0167`) and rank-1 (`1/61 ≈ 0.0164`) are close, so a single leg cannot dominate; cross-leg agreement must accumulate to win. A smaller `k` would make the #1 of any single leg overpowering; `60` is the well-known TREC default and is exposed as a tunable (`retrieval_rrf_k`) rather than hard-coded.

### 4.4 Key properties

- **Scale-free:** only ranks enter the sum, so legs need not be normalized against each other.
- **Additive reinforcement:** appearing in N legs adds N terms — agreement is rewarded monotonically.
- **Bounded contribution:** each leg-appearance adds at most `1/k`, so no single leg can swamp the fusion.
- **Stable:** missing from a leg simply contributes nothing (no penalty term), so partial coverage (e.g. RAG forbidden) degrades gracefully.

---

## 5. Citation → entity mapping via `doc_id`

The hybrid value comes from welding a dense *chunk* back to the graph *entity* it describes. cypherx-a1 records, at ingest time, exactly one `doc_id` per graph node in `cypherx_a1.citations`, making **`doc_id` the stable citation key**.

### 5.1 The back-map query

After collecting `rag_hits`, the orchestrator gathers their `doc_id`s and resolves them in a fresh tenant transaction via `ingest_repo.entities_for_docs`:

```sql
SELECT DISTINCT ON (c.doc_id) c.doc_id, e.entity_id, e.kind, e.natural_key, e.title
  FROM cypherx_a1.citations c
  JOIN cypherx_a1.entities e ON e.entity_id = c.entity_id AND e.valid_to IS NULL
 WHERE c.doc_id = ANY(%s)
```

`DISTINCT ON (c.doc_id)` collapses to one entity per doc; the join's `valid_to IS NULL` guarantees the back-map points at the *current* entity slice even if the doc was ingested against an older version. The result is `doc_entity: dict[doc_id → {entity_id, kind, natural_key, title}]`. (A parallel `entities_for_chunks` exists for `chunk_id`-keyed mapping; the orchestrator uses the `doc_id` path.)

### 5.2 The promotion logic

When folding rag hits into the RRF registry, each hit is routed by whether its `doc_id` resolves to a graph entity:

```python
for rank, h in enumerate(rag_hits):
    ent = doc_entity.get(h.get("doc_id", ""))
    if ent:
        key = f"entity:{ent['entity_id']}"
        item = registry.setdefault(key, _entity_item(key, ent))
        item.kind = "chunk"        # promote: we now have actual text
        item.doc_id = h.get("doc_id")
        item.chunk_id = h.get("chunk_id")
        item.source = item.source or "rag"
        item.uri = item.uri or h.get("source_uri")
    else:
        key = f"chunk:{h.get('chunk_id')}"
        item = registry.setdefault(key, EvidenceItem(key=key, kind="chunk", …))
    if not item.snippet:
        item.snippet = (h.get("content") or "")[:600]
    item.best_dense_score = max(item.best_dense_score or 0.0, float(h.get("score") or 0.0))
    _bump(key, rank)
```

Two outcomes:

| Case | Registry key | Effect |
| --- | --- | --- |
| **`doc_id` maps to an entity** | `entity:<entity_id>` (merges with any graph/keyword hit for the same entity) | the entity is **promoted** to `kind="chunk"` (it now has real chunk text + a `doc_id`/`chunk_id`), and its RRF score gains the dense leg's contribution. This is the welding step. |
| **`doc_id` has no entity** | `chunk:<chunk_id>` | a standalone chunk evidence item — still cited, still RRF-scored, just not entity-anchored. |

Either way the chunk's text fills `snippet` (first 600 chars) if empty, and `best_dense_score` keeps the **max** dense score seen for that key (multiple chunks of the same doc reinforce one entity).

### 5.3 The evidence registry

Fusion accumulates into one `registry: dict[str, EvidenceItem]` keyed by `entity:<id>` or `chunk:<id>`, so the *same* entity surfaced by graph + keyword + a dense chunk is exactly one row whose `rrf_score` is the sum of all three contributions. `registry.setdefault(...)` ensures first-seen wins for the base item; subsequent legs only `_bump` the score (and, for rag, enrich text/uri).

The `EvidenceItem` dataclass:

| Field | Meaning |
| --- | --- |
| `key` | dedup key (`entity:…` / `chunk:…`) |
| `kind` | `"entity"` or `"chunk"` (promoted to `chunk` when dense text attaches) |
| `title`, `snippet`, `source`, `uri` | display + provenance |
| `entity_id`, `entity_kind`, `natural_key` | graph anchor (when entity-backed) |
| `doc_id`, `chunk_id` | RAG anchor (when chunk-backed) |
| `rrf_score` | the fused score (sort key) |
| `best_dense_score` | max dense similarity seen (surfaced as `Citation.score`) |

---

## 6. Token-bounded context builder

After fusion the items are ranked and sliced:

```python
ordered = sorted(registry.values(), key=lambda it: it.rrf_score, reverse=True)
items = ordered[: s.retrieval_context_max_chunks]
```

`retrieval_context_max_chunks` (default **12**) caps how many evidence items can enter the prompt — a coarse, count-based bound applied *before* the char-based bound.

The fine bound lives in `RetrievalResult.context_text(max_chars=8000)`:

```python
def context_text(self, max_chars: int = 8000) -> str:
    parts, total = [], 0
    for it in self.items:
        block = f"[{it.title}]"
        if it.snippet:
            block += f"\n{it.snippet}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)
```

Behaviour, precisely:

- Items are consumed **in RRF order** (highest-scored first), so when the budget is hit, the *least* relevant evidence is the part dropped.
- The budget is **all-or-nothing per block**: a block that would push `total` past `max_chars` causes an immediate `break` — it is not partially truncated, and nothing after it is added. (This is a char proxy for a token budget; `max_chars ≈ 8000` ≈ a few thousand tokens, leaving headroom under `copilot_max_tokens`.)
- Each block is `[title]\n snippet`; blocks are joined with a blank line. Empty-snippet items contribute just `[title]`.

The copilot (`copilot/service.py`, step 4) calls `retrieval.context_text()` and assembles the user message as: optional memory recall, then `Context from engineering memory:\n{context or '(no matching context found)'}`, then `Question: …`. The system prompt instructs the model to answer **using ONLY the provided context** and to say so plainly when context is insufficient — the token bound therefore directly shapes answer grounding.

### 6.1 Citations are emitted independently

`RetrievalResult.citations()` converts **every** surviving `EvidenceItem` into a `Citation` (wire model in `models/api.py`), regardless of whether its block fit in `context_text`'s char budget — so the answer's provenance list can be richer than the prose context, and **answers are never uncited**:

```python
Citation(
    kind="chunk" if it.kind == "chunk" else "entity",
    title=it.title, source=it.source, uri=it.uri,
    entity_id=it.entity_id, entity_kind=it.entity_kind, natural_key=it.natural_key,
    doc_id=it.doc_id, chunk_id=it.chunk_id,
    score=it.best_dense_score,
    snippet=(it.snippet[:240] or None) if it.snippet else None,
)
```

`Citation.score` carries the **dense** score (not the RRF score), and `snippet` is clamped to 240 chars for the citation payload. The `AskResponse` returns `citations`, plus `used` — the leg-count summary `{"graph": …, "keyword": …, "rag": …}` from `RetrievalResult.used` — so a caller (or autonomous agent) can see how each leg contributed.

---

## 7. Graph reasoning algorithms

The `/v1/graph/*` endpoints and the `mcp-eng-memory` MCP tools answer the core questions **deterministically with no LLM call**, straight from the graph. They are the structured complement to the copilot's prose. Each is a thin wrapper in `copilot/queries.py` (`GraphQueryService`) over a recursive/aggregate query in `graph_repo.py`, returning structured `items` + `Citation` provenance.

| Question | REST endpoint | Service method | graph_repo query | Algorithm |
| --- | --- | --- | --- | --- |
| Who owns X? | `POST /v1/graph/who-owns` | `who_owns` | `owners_of` | reverse ownership-relation aggregate |
| What breaks if I change X? | `POST /v1/graph/what-breaks` | `what_breaks_if_changed` | `impact_of` | recursive reverse-`depends_on` CTE |
| Who are the experts on T? | `POST /v1/graph/experts` | `experts_on` | `experts_on` | topic FTS → ownership-signal aggregate |
| Why was X built? | `POST /v1/graph/why-built` | `why_built` | `find_entities` (decision/PR/ticket kinds) | FTS over artifact kinds |
| What's adjacent to X? | `POST /v1/graph/neighbors` | `neighbors` | `neighbors` | one-hop typed traversal (both directions) |

All endpoints require the relevant `query` scope (`graph:who-owns`, `graph:what-breaks`, …), take identity only from the verified JWT (request models are `extra="forbid"`), and run inside an `in_tenant` RLS transaction.

### 7.1 Target resolution

Three of the tools first resolve a free-text `target` to a single entity via `_resolve_target` → `find_entities(..., limit=1)` with a **kind filter** scoped to the question:

| Tool | Allowed kinds for resolution |
| --- | --- |
| `who_owns` | `repo`, `service`, `feature`, `document` |
| `what_breaks_if_changed` | `service`, `repo`, `feature`, `document` |
| `neighbors` | (no filter — any kind) |

If resolution returns nothing, the tool returns `([], [])` (empty items + citations) rather than erroring — an unknown target is a clean "no answer", not a 5xx.

### 7.2 `who_owns` — ownership aggregate

`graph_repo.owners_of` finds **people** connected *into* the target by an ownership-ish relation, ranked by signal:

```sql
SELECT p.entity_id, p.natural_key, p.title, p.attrs,
       array_agg(DISTINCT e.rel) AS rels,
       max(e.confidence)          AS confidence,
       count(*)                   AS signal,
       (array_agg(e.evidence_chunk_ids ORDER BY e.confidence DESC))[1] AS evidence_chunk_ids
  FROM cypherx_a1.edges e
  JOIN cypherx_a1.entities p
    ON p.entity_id = e.src_entity_id AND p.kind = 'person'
 WHERE e.dst_entity_id = %(eid)s
   AND e.rel = ANY(%(rels)s)
   AND e.valid_to IS NULL AND p.valid_to IS NULL
 GROUP BY p.entity_id, p.natural_key, p.title, p.attrs
 ORDER BY confidence DESC, signal DESC
 LIMIT %(limit)s
```

- **`rels = OWNERSHIP_RELS = ("owns", "authored", "reviewed", "expert_in")`** — the canonical ownership-signal relation set.
- Edges point **person → target** (`src = person`, `dst = target`), so this reads `e.dst_entity_id = target`.
- Ranking is `max(confidence)` then `count(*)` (how many distinct ownership edges) — a person who *owns* and *authored* and *reviewed* the repo outranks a one-edge author.
- `evidence_chunk_ids` from the highest-confidence edge rides along for provenance.

`who_owns` shapes each owner into `{person, natural_key, relations, confidence, signal}` and cites both the resolved target entity and every owner entity.

### 7.3 `what_breaks_if_changed` — recursive blast radius

The headline reasoning query. `graph_repo.impact_of` computes the **reverse-`depends_on` transitive closure** — everything that (directly or transitively) depends on the target — with a recursive CTE bounded by `max_hops`:

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
SELECT n.entity_id, n.kind, n.natural_key, n.title, n.attrs, min(b.depth) AS depth
  FROM blast b
  JOIN cypherx_a1.entities n ON n.entity_id = b.entity_id AND n.valid_to IS NULL
 GROUP BY n.entity_id, n.kind, n.natural_key, n.title, n.attrs
 ORDER BY depth ASC
 LIMIT %(limit)s
```

How it works, precisely:

1. **Anchor:** all `src` nodes with a `depends_on` edge pointing *at* the target — i.e. the direct dependents — at `depth = 1`.
2. **Recursion:** for each frontier node, find *its* direct dependents and increment `depth`, stopping at `b.depth < max_hops`.
3. **`UNION`** (not `UNION ALL`) dedupes within the recursion, so the CTE terminates even on cyclic dependency graphs.
4. **`min(b.depth)`** in the outer aggregate reports the *shortest* path to each impacted node (a node reachable at depth 1 and depth 3 is reported as depth 1 — its nearest blast distance).
5. `valid_to IS NULL` on both the edges and the final entity join keeps the traversal on the **current** bitemporal slice.
6. Results are ordered nearest-first (`depth ASC`).

`max_hops` comes from the request (`WhatBreaksRequest.max_hops`, default 3, clamped `1..6`; the platform default is `retrieval_max_hops = 3`). For each impacted node the service additionally calls `owners_of(..., limit=3)` so the answer says *what* breaks **and** *who to call* — every blast item is `{entity, kind, natural_key, depth, owners[]}`, cited per entity.

> **Why a recursive CTE and not a graph extension?** The locked decision is an **adjacency-list + recursive-CTE graph on the frozen `pgvector/pgvector:pg16` image** — no Apache AGE, no `ltree`; the runtime role `cxa1_user` cannot `CREATE EXTENSION`. The `GraphRetriever` seam keeps a later AGE/Neo4j swap from touching any SharedCore. `impact_of` is the load-bearing proof that pure SQL recursion is sufficient for the MVP's impact-analysis.

### 7.4 `experts_on` — topic FTS → signal aggregate

`graph_repo.experts_on` is a two-stage query: FTS to find the topic's entities, then aggregate ownership signal from people into those entities.

```sql
WITH topic_nodes AS (
    SELECT entity_id FROM cypherx_a1.entities
     WHERE valid_to IS NULL
       AND (fts @@ plainto_tsquery('english', %(topic)s) OR natural_key = %(topic)s)
     LIMIT 200
)
SELECT p.entity_id, p.natural_key, p.title, p.attrs,
       count(*)          AS signal,
       sum(e.confidence) AS score,
       array_agg(DISTINCT e.rel) AS rels
  FROM cypherx_a1.edges e
  JOIN topic_nodes tn ON tn.entity_id = e.dst_entity_id
  JOIN cypherx_a1.entities p ON p.entity_id = e.src_entity_id AND p.kind = 'person'
 WHERE e.rel = ANY(%(rels)s) AND e.valid_to IS NULL AND p.valid_to IS NULL
 GROUP BY p.entity_id, p.natural_key, p.title, p.attrs
 ORDER BY score DESC, signal DESC
 LIMIT %(limit)s
```

- **Stage 1 `topic_nodes`:** up to 200 current entities matching the topic by FTS (or exact `natural_key`).
- **Stage 2:** join people → those topic nodes over `OWNERSHIP_RELS`, aggregating `sum(confidence)` as **`score`** and `count(*)` as **`signal`**.
- Ranking is `score DESC, signal DESC` — total confidence-weighted involvement wins, ties broken by raw edge count.

Note the difference from `owners_of`: experts ranks by **sum** of confidence across *many topic nodes* (breadth of involvement in a subject area), whereas `owners_of` ranks by **max** confidence on a *single* target (strength of ownership of one thing). `experts_on` shapes each expert as `{person, natural_key, relations, score, signal}`.

### 7.5 `neighbors` — one-hop typed traversal

`graph_repo.neighbors` returns one-hop typed neighbours; the tool calls it with `direction="both"`:

```sql
SELECT n.entity_id, n.kind, n.natural_key, n.title, n.attrs,
       e.rel, e.confidence, e.evidence_chunk_ids, e.edge_id
  FROM cypherx_a1.edges e
  JOIN cypherx_a1.entities n ON {join}     -- out | in | both
 WHERE e.valid_to IS NULL AND n.valid_to IS NULL {rel_clause}
 ORDER BY e.confidence DESC
 LIMIT %(limit)s
```

`direction` selects the join predicate (`out`: `e.src = eid`; `in`: `e.dst = eid`; `both`: either). An optional `rels` tuple adds `AND e.rel = ANY(...)`. Ranked by edge `confidence`.

---

## 8. Performance & correctness notes

- **Indexes the legs rely on** (`db/migrations/…__init.sql`): a GIN index on `entities.fts` (both FTS legs), the partial unique index `(tenant_id, kind, natural_key) WHERE valid_to IS NULL` (stable `entity_id`, natural-key match), and the edge indexes `(tenant, src, rel)` + `(tenant, dst, rel)` + the partial `WHERE valid_to IS NULL` (the recursive CTEs and ownership aggregates). The recursive `impact_of` walks `e.dst_entity_id = b.entity_id`, served by the `(tenant, dst, rel)` index.
- **RLS everywhere:** every read runs through `in_tenant(pool, tenant_id, fn)`, which `SET LOCAL app.tenant_id` for the transaction; `cxa1_user` has no `BYPASSRLS`. The `current_setting('app.tenant_id', true)` + `NULLIF` guard means a missing tenant binds to `NULL` (matches nothing) rather than leaking — cross-tenant retrieval is architecturally impossible.
- **Bitemporality:** every leg and every reasoning query filters `valid_to IS NULL`, so retrieval always reflects the *current* slice; superseded versions are invisible without explicitly asking for history.
- **Termination:** `impact_of`/`experts_on` use `UNION` (dedup) + an explicit depth/row bound (`max_hops`, `LIMIT`, `topic_nodes LIMIT 200`) so a pathological graph cannot blow up the query.
- **Fail-soft retrieval:** a forbidden KB is skipped (not fatal); a RAG outage raises `SERVICE_UNAVAILABLE`. In the copilot, retrieval feeds a fail-closed guardrail flow (`block` → `422 GUARDRAIL_VIOLATION`) and best-effort memory (an outage never fails an answer).

---

## 9. Tuning knobs

All in `src/cypherx_a1/core/config.py` (pydantic-settings, env-overridable, Doppler-injected):

| Setting | Default | Effect |
| --- | --- | --- |
| `retrieval_graph_limit` | `20` | leg-1 (`find_entities`) result cap |
| `retrieval_keyword_limit` | `20` | leg-3 (`keyword_search`) result cap |
| `retrieval_rrf_k` | `60` | RRF constant `k` in `1/(k+rank)` — higher flattens leg dominance |
| `retrieval_context_max_chunks` | `12` | max evidence items entering the prompt (pre-char-budget) |
| `retrieval_max_hops` | `3` | default blast-radius depth (request can override `1..6`) |
| `rag_query_top_k` | `20` | clamp; per-request `top_k` (`AskRequest.top_k` ≤ 50) still applies, RAG caps at 100 |
| `rag_query_ef_search` | `100` | HNSW `ef_search` (clamped to ≤ 500) |
| `rag_query_min_score` | `0.0` | dense-score floor |
| `rag_embedding_model` | `text-embedding-3-small` | **pinned** model alias for KBs (never the repointable `embed` alias) |
| `copilot_model` | `smart` | llms-gateway model alias for the answer |
| `copilot_max_tokens` | `1024` | answer length budget (pairs with the ~8000-char context bound) |
| `copilot_temperature` | `0.2` | low — grounded, deterministic answers |

`context_text(max_chars=8000)` is a method default, not a config field; change it at the call site (`CopilotService.ask` step 4) if a larger context window is wanted.

---

## 10. End-to-end summary

1. The copilot recalls episodic memory, PRE-guardrails the question, then calls `RetrievalOrchestrator.retrieve`.
2. **Leg 1 (graph)** + **Leg 3 (keyword)** run in one tenant tx over `cypherx_a1.entities.fts`; the same tx lists the tenant's KB ids from `cypherx_a1.rag_kbs`.
3. **Leg 2 (rag-dense)** queries each KB over `POST /v1/kbs/{kb_id}/query` (`search_mode="dense"`, clamped `top_k`/`ef_search`), skipping forbidden KBs.
4. RAG `doc_id`s are back-mapped to graph entities via `cypherx_a1.citations` (`entities_for_docs`), **welding** chunks to entities.
5. **RRF** (`Σ 1/(k+rank)`, `k=60`) fuses all three legs into one `EvidenceItem` registry; items are sorted by `rrf_score` and sliced to `retrieval_context_max_chunks`.
6. `context_text()` builds a **token-bounded** prompt context (highest-RRF-first, ~8000 chars); `citations()` emits a `Citation` for **every** surviving item — answers are never uncited.
7. In parallel, the **graph reasoning** tools (`who_owns`/`what_breaks`/`experts_on`/`why_built`/`neighbors`) answer the same questions *deterministically* over `graph_repo` recursive CTEs + aggregates, backing both `/v1/graph/*` and the `mcp-eng-memory` MCP server.

Everything here is app-owned and SharedCore-respecting: keyword + RRF + back-map + reasoning live in cypherx-a1; RAG is consumed dense-only via its versioned `/v1` contract; the graph never enters RAG or Memory.
