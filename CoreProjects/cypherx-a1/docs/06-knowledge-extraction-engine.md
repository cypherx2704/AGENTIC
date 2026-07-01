# Knowledge extraction engine

> The LLM enrichment pass: read engineering artifacts the deterministic ingest already landed, ask the llms-gateway (JSON-object mode) for the relationships raw ingest cannot see (`depends_on` / `decided_in` / `caused` / `resolved` / `expert_in` / `mentions`), and write them as confidence-scored, evidence-bearing edges that **supersede** prior versions bitemporally — keyed for idempotency and cost so re-ingest never re-spends.

---

## 1. Why an extraction pass exists

Deterministic ingest (`ingestion/normalizer.py` + `ingestion/pipeline.py`) gives us the **structural** graph for free: who authored a PR, who reviewed it, which repo a PR is `part_of`, who `owns` a repo. Those edges come straight from the GitHub payload — no model required, and the keyless fixtures bake explicit `owns`/`depends_on` edges so `who_owns` / `what_breaks` answer without an LLM.

What ingest **cannot** see is the *semantic* relationships buried in artifact prose:

- a PR description that says "this unblocks the migration once `billing-svc` ships" → `depends_on(billing-svc)`
- an incident postmortem that says "root cause was the cache eviction change in PR #412" → `caused` / `resolved`
- a ticket that reads "decided in the Q2 arch review to drop the monolith" → `decided_in`
- a doc that establishes someone as the go-to person for a subsystem → `expert_in`

The extraction engine is the single component that mines those edges. It is a **strict enrichment**: when no real provider is configured the explicit ingest edges already answer the demo queries, and extraction simply records the job (see §10). It does not replace ingest and never emits the deterministic relations.

The engine lives in `src/cypherx_a1/extraction/extractor.py`. Its public entry point is `run_extraction(...)`, invoked synchronously by `POST /v1/extract` (`api/connectors.py`) in the MVP; the Kafka worker (`worker/runner.py`) is a documented scale-out seam that calls the same function.

---

## 2. End-to-end flow

```
POST /v1/extract  (ingest scope)
        │
        ▼
run_extraction(pool, tenant_id, agent_jwt, agent_id, llms, settings, limit=50)
        │
        │  ev = settings.extractor_version            (e.g. "1.0.0")
        ▼
ingest_repo.list_unextracted_entities(conn, extractor_version=ev, limit)   ── RLS-scoped read
        │
        │  for each pending node:
        ▼
_extract_node(...)
        │
        ├─ build prompt:  system (_SYSTEM_PROMPT) + user (artifact title + search_text, ≤6000 chars)
        │
        ├─ llms.chat(model=extraction_model, response_format={"type":"json_object"},
        │             idempotency_key=sha256(tenant:node:content_sha:ev),
        │             agent_jwt=…, on_behalf_of=agent_id)            ── ONLY path to a provider
        │
        ├─ _parse_edges(completion.content)            ── tolerant JSON validation → list[dict]
        │
        └─ in_tenant(pool, tenant_id, _write):         ── single RLS transaction
              ├─ graph_repo.supersede_extracted_edges(src, ev)      ── bitemporal close of prior versions
              ├─ for each edge:
              │     ├─ graph_repo.get_entity_id(kind, target_key)  (else upsert_entity source="extracted")
              │     └─ graph_repo.upsert_edge(src→dst, rel, confidence, ev, metadata={"evidence":…})
              └─ ingest_repo.record_extraction_job(node, content_sha, ev,
                      edges_extracted, llm_call_id, cost_usd)        ── idempotency + cost ledger
        ▼
ExtractResponse(nodes_seen, nodes_extracted, edges_added, failed)
```

Per-node failures are caught and counted — **one node's failure must not abort the pass** (`except Exception` around `_extract_node`, incrementing `stats.failed`). The pass returns a tally either way.

---

## 3. What is eligible for extraction

The driver query `ingest_repo.list_unextracted_entities` selects **current** entities that:

1. have a non-null `content_sha` (`e.content_sha IS NOT NULL`), and
2. are of an artifact kind that carries prose — `kind IN ('pr','ticket','incident','decision','document')`, and
3. have **no** completed `extraction_jobs` row at the current `extractor_version` for that exact `content_sha`.

```sql
SELECT e.entity_id, e.kind, e.natural_key, e.title, e.search_text, e.content_sha
  FROM cypherx_a1.entities e
 WHERE e.valid_to IS NULL AND e.content_sha IS NOT NULL
   AND e.kind IN ('pr','ticket','incident','decision','document')
   AND NOT EXISTS (
       SELECT 1 FROM cypherx_a1.extraction_jobs j
        WHERE j.node_id = e.entity_id AND j.content_sha = e.content_sha
          AND j.extractor_version = %s AND j.status = 'completed')
 LIMIT %s
```

Note what is **excluded**: `person`, `service`, `repo`, and `feature` nodes are never *sources* of extraction (they have no narrative body to mine), though they are valid *targets* of extracted edges (§4). The `limit` defaults to `50` from `run_extraction`'s signature (the repo function defaults `100` but the caller passes `50`); a single `/v1/extract` call processes at most one page, so a backlog is drained by repeated calls (or, at scale, by the worker).

---

## 4. The extraction contract: prompt + JSON schema

### 4.1 System prompt

A frozen system prompt (`_SYSTEM_PROMPT`) constrains the model to a strict JSON object. It is reproduced verbatim from the code:

```
You extract a software-engineering knowledge graph from one artifact. Return STRICT JSON:
{"edges": [{"rel": <one of depends_on|decided_in|caused|resolved|expert_in|mentions>,
"target_kind": <one of service|repo|feature|decision|incident|person|document|pr|ticket>,
"target_key": <stable natural key, e.g. a service name or 'owner/repo'>,
"confidence": <0..1>, "evidence": <short quote>}]}.
Only include edges strongly supported by the text. If none, return {"edges": []}.
```

### 4.2 User message

```
Artifact (<kind> <natural_key>):
<title>

<search_text>
```

The artifact text is `title` + `"\n\n"` + `search_text`, stripped and **truncated to 6000 characters** (`[:6000]`). This is a deliberate cost/latency clamp; long bodies are not chunked for extraction (chunking is RAG's job for retrieval, not extraction).

### 4.3 Gateway call parameters

| Parameter | Value | Source |
| --- | --- | --- |
| `model` | `settings.extraction_model` (default `"smart"` alias) | `config.py` |
| `max_tokens` | `settings.extraction_max_tokens` (default `1024`) | `config.py` |
| `temperature` | `settings.extraction_temperature` (default `0.0`) | `config.py` |
| `response_format` | `{"type": "json_object"}` | `extractor.py` |
| `stream` | `False` (forced by `LlmsClient.chat`) | `llms_client.py` |
| `idempotency_key` | `sha256(tenant:node:content_sha:ev)` | `_idem_key` |

`temperature=0.0` makes extraction maximally deterministic so the idempotency key has teeth (same artifact + same version → same edges). The call goes to `POST {LLMS_GATEWAY_URL}/v1/chat/completions` — **the only path to a provider** (Contract boundary). Identity travels in headers only (`Authorization: Bearer <service-jwt>`, `X-Forwarded-Agent-JWT: <agent-jwt>`, W3C trace); the body carries no identity.

### 4.4 Allowed extracted relations

The LLM may emit **only** these six relations — `_EXTRACTABLE_RELS`:

| `rel` | Semantic | Typical source kind |
| --- | --- | --- |
| `depends_on` | the artifact's subject relies on the target (also powers the reverse-blast-radius "what breaks if I change X" query) | pr, ticket, document |
| `decided_in` | a decision was reached in / recorded by the target | ticket, decision |
| `caused` | the source caused the target (e.g. a change caused an incident) | pr, incident |
| `resolved` | the source resolved the target | pr, incident |
| `expert_in` | the source person has expertise in the target | document, person |
| `mentions` | a weak co-reference link | any |

The deterministic relations `owns`, `authored`, `reviewed`, `part_of`, `deployed` come from **ingest, not the LLM**, and are explicitly excluded from `_EXTRACTABLE_RELS`. The DB-level `edges_rel_enum` CHECK constraint is the wider union of both sets (`'owns','authored','reviewed','depends_on','caused','resolved','mentions','decided_in','deployed','expert_in','part_of'`), so the extractor's narrower allow-list is the *enforced* policy at write time — any other relation the model invents is dropped by `_parse_edges` before it can reach the constraint.

### 4.5 Allowed target kinds

`_TARGET_KINDS` = `{service, repo, feature, decision, incident, person, document, pr, ticket}` — the full entity vocabulary minus nothing relevant; these match the `entities_kind_enum` CHECK on `cypherx_a1.entities`.

### 4.6 Parsing + validation (`_parse_edges`)

`_parse_edges(content)` is **tolerant by design** — a malformed or non-JSON response yields `[]` rather than raising, so the job is still recorded and never retried forever (the mock-provider case in §10 relies on this). The validation gate, per edge:

1. `json.loads(content)` — on `ValueError`/`TypeError` → return `[]`.
2. top-level must be a `dict` with an `edges` key that is a `list`; otherwise `[]`.
3. each item must be a `dict`; non-dicts skipped.
4. `rel` must be in `_EXTRACTABLE_RELS`; `target_kind` must be in `_TARGET_KINDS`; `target_key` must be non-empty after strip — otherwise the edge is **dropped**.
5. `confidence` is coerced `float` and **clamped to `[0.0, 1.0]`**; a non-numeric value defaults to `0.5`.
6. `evidence` is coerced to `str` and **truncated to 500 chars**.

The validated shape passed downstream is `{"rel", "target_kind", "target_key", "confidence", "evidence"}`. This belt-and-suspenders validation means a hallucinated relation, an unknown kind, or a missing key can never corrupt the graph — the worst case is fewer edges.

---

## 5. Confidence & evidence model

Every extracted edge carries two provenance signals that distinguish it from a deterministic ingest edge:

| Signal | Where it lives | Meaning |
| --- | --- | --- |
| `confidence` | `cypherx_a1.edges.confidence` `NUMERIC(4,3)` | the model's `0.000..1.000` strength; defaults to `0.5` when the model omits/garbles it. Ingest edges default to `1.000`. |
| evidence quote | `cypherx_a1.edges.metadata->>'evidence'` (JSONB) | a short verbatim quote (≤500 chars) supporting the edge, written as `metadata={"evidence": e.get("evidence", "")}` in `upsert_edge`. |

Note the structural asymmetry: extraction stores its evidence as a **text quote in `metadata`**, whereas the separate `edges.evidence_chunk_ids UUID[]` column holds RAG chunk IDs for citation back-mapping and is left at its default `'{}'` by the extractor (`upsert_edge`'s `evidence_chunk_ids` defaults to `[]` when not passed). The extractor passes only `metadata`, never `evidence_chunk_ids`.

Confidence is **consumed downstream**, it is not cosmetic:

- `graph_repo.neighbors(...)` orders one-hop results `ORDER BY e.confidence DESC`.
- `owners_of(...)` ranks people by `max(e.confidence)` then signal count.
- `experts_on(...)` ranks by `sum(e.confidence)` (aggregate confidence × frequency).

So a low-confidence extracted edge naturally sinks below high-confidence (ingest = `1.000`) edges in every ranked query, which is the implicit guardrail against weak extractions polluting answers.

---

## 6. `extractor_version` & bitemporal supersede

`extractor_version` (default `"1.0.0"`, `settings.extractor_version`) is the version stamp on every extracted edge and the partition key of the idempotency ledger. Its job is to make a **model/prompt bump supersede prior versions instead of duplicating** them.

### 6.1 Supersede-before-write

Before writing fresh edges for a node, `_write` calls:

```python
await graph_repo.supersede_extracted_edges(conn, src_entity_id=node_id, extractor_version=ev)
```

which bitemporally closes the prior **extracted** slice:

```sql
UPDATE cypherx_a1.edges
   SET valid_to = NOW()
 WHERE src_entity_id = %s
   AND valid_to IS NULL
   AND extractor_version <> 'ingest'        -- never touch deterministic ingest edges
   AND extractor_version <> %s              -- never touch the same-version current slice
```

Two guards make this safe:

- **`extractor_version <> 'ingest'`** — deterministic ingest edges (stamped `'ingest'`, the column's DEFAULT) are *never* closed by extraction. Ingest and extraction own disjoint slices of the same node's out-edges.
- **`extractor_version <> %s`** — the *current* version's own edges are left alone, so a re-run at the same version that re-upserts is idempotent rather than self-closing.

The net effect: old extracted edges get `valid_to = NOW()` (they fall out of every `valid_to IS NULL` read but remain for audit/time-travel), and the new version's edges become the current slice. The graph is bitemporal — nothing is deleted.

### 6.2 Per-edge write (`upsert_edge`)

For each validated edge:

1. **Resolve the target.** `graph_repo.get_entity_id(kind=target_kind, natural_key=target_key)`. If the target does not yet exist, it is created with `upsert_entity(source="extracted", content_sha=None, ...)` — a placeholder node whose `title`/`search_text`/`natural_key` are all the `target_key`. (When the real artifact for that key is later ingested, the partial-unique-index upsert merges into the same `entity_id`, so the placeholder is filled in, not duplicated.)
2. **Write the edge.** `upsert_edge(src→dst, rel, confidence, extractor_version=ev, metadata={"evidence":…})`. `upsert_edge` supersedes-in-place: it `UPDATE`s the matching current edge `(src, dst, rel) WHERE valid_to IS NULL` if present, else `INSERT`s — deterministic and idempotent for re-runs.

The `edges_extracted` count returned per node is the number of edges written (after parse-validation), which the API surfaces as `ExtractResponse.edges_added`.

---

## 7. Idempotency keys & cost metering

Two layers of idempotency, one cost ledger.

### 7.1 The gateway-call idempotency key (Contract 9)

Each `llms.chat` call carries an `Idempotency-Key` header computed by `_idem_key`:

```python
hashlib.sha256(f"{tenant_id}:{node_id}:{content_sha}:{ev}".encode()).hexdigest()
```

A retried worker (or a re-issued `/v1/extract`) with the same `(tenant, node, content_sha, extractor_version)` produces the **identical** key, so the gateway **replays** its cached completion instead of re-spending on the provider. Because `content_sha` is in the key, an edited artifact (new sha) is a *new* call — re-extraction follows content, not time.

### 7.2 The `extraction_jobs` ledger (idempotency + cost)

`ingest_repo.record_extraction_job` writes one row per processed node into `cypherx_a1.extraction_jobs`. The table's composite primary key **is** the idempotency contract:

| Column | Type | Role |
| --- | --- | --- |
| `tenant_id` | `UUID` | RLS / tenant scope (from `app.tenant_id`, not the body) |
| `node_id` | `UUID` | the entity the extraction ran over |
| `content_sha` | `TEXT` | the artifact content hash (re-ingest of unchanged content = same sha) |
| `extractor_version` | `VARCHAR(20)` | the model/prompt version |
| `status` | `VARCHAR(20)` | `completed` \| `failed` \| `running` (CHECK-constrained) |
| `edges_extracted` | `INTEGER` | edges written this pass |
| `llm_call_id` | `TEXT` | **the gateway's billing key** (Contract 19) |
| `cost_usd` | `NUMERIC(12,8)` | the gateway's cost number, recorded verbatim |
| **PK** | — | `(tenant_id, node_id, content_sha, extractor_version)` |

The `record_extraction_job` upsert is `ON CONFLICT (tenant_id, node_id, content_sha, extractor_version) DO UPDATE` — so a re-run overwrites the ledger row rather than duplicating it. Combined with the `NOT EXISTS (... status = 'completed')` filter in `list_unextracted_entities`, a node that completed at the current version is **never re-selected** → re-ingest never re-spends.

### 7.3 Cost is the gateway's, never rewritten (Contract 19)

`cost_usd` and `llm_call_id` are read straight off the gateway response (`completion.usage.cost_usd`, `completion.llm_call_id`) — see `LlmsClient._parse_chat`, which pulls `usage.cost_usd` and `llm_call_id` (falling back to `id`) from the response body. cypherx-a1 **records** these numbers but **never computes or rewrites** them. The gateway owns metering; this service owns only its own ledger. The app's own usage event is emitted separately on `cypherx.cypherxa1.usage.recorded` (its OWN topic, Contract 19), never by rewriting the gateway's figures.

### 7.4 Observability

`run_extraction` increments the Prometheus counter `cypherxa1_extraction_jobs_total{outcome}`:

- `outcome="completed"` per node that extracted successfully,
- `outcome="failed"` per node whose `_extract_node` raised.

(`outcome="skipped"` is a declared label value, reserved for skip paths.) Per-node failures also log `extraction_node_failed` with `node_id` + `error` via structlog (Contract 6 JSON).

---

## 8. The `/v1/extract` endpoint

| Property | Value |
| --- | --- |
| Method + path | `POST /v1/extract` |
| Router | `api/connectors.py` → `extract(...)` |
| Auth | agent JWT (re-verified against Auth JWKS) → `require_principal` |
| Scope | `require_scope(principal, ingest_scopes(), "extract:run")` |
| Request body | none |
| Response model | `ExtractResponse` (`models/api.py`, `_Resp` base, `extra="forbid"`) |

`ExtractResponse` fields (all `int`), mapped 1:1 from `ExtractionStats`:

| Field | Meaning |
| --- | --- |
| `nodes_seen` | candidate nodes pulled by `list_unextracted_entities` this call |
| `nodes_extracted` | nodes whose extraction completed (job recorded) |
| `edges_added` | total edges written across all nodes |
| `failed` | nodes whose `_extract_node` raised |

`ExtractionStats` also tracks `skipped` internally; it is not surfaced on the response.

Identity flows from the verified principal into `run_extraction`: `tenant_id=principal.tenant_id`, `agent_jwt=principal.raw_token` (forwarded to the gateway as `X-Forwarded-Agent-JWT`), `agent_id=principal.agent_id` (becomes the gateway `on_behalf_of`). The tenant is **always** from the token, never a body field — and the whole `_write` runs inside `in_tenant(pool, tenant_id, ...)`, so every `INSERT`/`UPDATE` is RLS-scoped to that tenant via `app.tenant_id`.

---

## 9. Source-of-truth conflict policy

The graph is a single shared graph per tenant, written by two producers — **deterministic ingest** and **LLM extraction**. They are kept from fighting by a strict ownership split, not by last-writer-wins:

| Rule | Mechanism |
| --- | --- |
| Ingest owns the structural relations | `owns`/`authored`/`reviewed`/`part_of`/`deployed` are never in `_EXTRACTABLE_RELS`; the LLM cannot emit them, and `_parse_edges` drops them if it tries. |
| Extraction owns the semantic relations | `depends_on`/`decided_in`/`caused`/`resolved`/`expert_in`/`mentions` from prose. |
| Extraction never closes ingest edges | `supersede_extracted_edges` filters `extractor_version <> 'ingest'`. |
| Ingest edges win at read time | ingest `confidence` = `1.000` vs extracted's model score (≤ `1.0`), and every ranked read orders by confidence descending. A weak extracted edge cannot outrank a deterministic fact. |
| Re-extraction supersedes, never duplicates | bitemporal `valid_to` close of the prior extracted slice + same-`(src,dst,rel)` upsert-in-place. |
| Entities merge, not collide | placeholder `source="extracted"` nodes and real `source="github"` nodes converge on the partial-unique index `(tenant_id, kind, natural_key) WHERE valid_to IS NULL` → same `entity_id`. |

The `entities.source` column records provenance (`github`, `extracted`, …) so an operator can always tell a placeholder created by extraction from a fully-ingested node. Nothing is destructively overwritten: superseded slices keep a non-null `valid_to` for audit and time-travel.

---

## 10. Keyless / mock-provider behaviour

cypherx-a1 runs fully offline by default (`CONNECTOR_MODE=mock` plus upstream `MOCK_PROVIDERS`). In that mode the llms-gateway returns a **canned completion** that is not useful JSON. The extraction engine is built to degrade cleanly:

1. `_parse_edges` gets a non-JSON / non-`{"edges":[...]}` body → returns `[]` (no raise).
2. `_extract_node` therefore writes **0 edges**, but still calls `record_extraction_job(... edges_extracted=0, llm_call_id=…, cost_usd=…)`.
3. Because the job is recorded `completed`, the node drops out of `list_unextracted_entities` and is **not retried forever**.

So in keyless mode `/v1/extract` is effectively a no-op enrichment that costs nothing: the explicit `owns`/`depends_on` edges baked into the GitHub fixtures already satisfy `who_owns` / `what_breaks`, and extraction becomes meaningful only when a real provider is configured. This is the intended posture — **extraction is a strict enrichment, never a correctness dependency for the demo queries.**

---

## 11. Human-in-the-loop for low confidence

The MVP writes every validated edge regardless of confidence and **leans on confidence-ordered reads** rather than a hard write-time threshold, so weak edges exist but are demoted in every ranked query (§5). This is the lightweight first-cycle policy. The data model is, however, already shaped for a richer human-in-the-loop (HITL) workflow without a schema change:

- Each extracted edge carries its `confidence` and a human-readable `metadata.evidence` quote — exactly the two fields a reviewer needs to accept or reject an edge.
- Because writes are bitemporal, a rejected edge is closed (`valid_to = NOW()`) rather than deleted, preserving the decision trail.
- `extraction_jobs.status` already allows `running` alongside `completed`/`failed`, leaving room for a `review`-style intermediate state.

A future HITL slice would gate edges below a configurable confidence floor into a review queue (surfaced to the copilot/admin), promoting only accepted edges into the current slice. The seam is present today; the gate is intentionally not yet enabled (consistent with the MVP's "strict enrichment" stance and the confidence-ordered read defense).

---

## 12. Configuration reference

All knobs from `core/config.py` (env-driven, no prefix, Doppler-compatible):

| Setting | Default | Purpose |
| --- | --- | --- |
| `extraction_model` | `"smart"` | gateway model alias for the extraction chat call |
| `extractor_version` | `"1.0.0"` | version stamp + idempotency-ledger partition; bump to supersede |
| `extraction_max_tokens` | `1024` | completion cap |
| `extraction_temperature` | `0.0` | deterministic extraction (gives the idempotency key teeth) |
| `llms_gateway_url` | `http://localhost:8085` | base for `POST /v1/chat/completions` |
| `llms_timeout_seconds` | `120.0` | extraction round-trip timeout |

To **re-mine the whole corpus with a better model/prompt**: change `extraction_model` and/or the prompt, bump `extractor_version` (e.g. `1.0.0` → `1.1.0`), redeploy, then call `/v1/extract` until `nodes_seen` reaches `0`. The version bump invalidates the ledger filter (new `extractor_version` has no `completed` rows), so every artifact is re-extracted exactly once; the new edges supersede the old extracted slice bitemporally, and ingest edges are untouched throughout.

---

## 13. Key tables, functions & symbols (quick map)

| Symbol | File | Role |
| --- | --- | --- |
| `run_extraction(...)` | `extraction/extractor.py` | pass driver; returns `ExtractionStats` |
| `_extract_node(...)` | `extraction/extractor.py` | per-node prompt → chat → parse → write |
| `_parse_edges(content)` | `extraction/extractor.py` | tolerant JSON validation, allow-list enforcement, confidence clamp |
| `_idem_key(...)` | `extraction/extractor.py` | `sha256(tenant:node:content_sha:ev)` Idempotency-Key |
| `_SYSTEM_PROMPT`, `_EXTRACTABLE_RELS`, `_TARGET_KINDS` | `extraction/extractor.py` | the extraction contract constants |
| `LlmsClient.chat(...)` | `services/llms_client.py` | `POST /v1/chat/completions`, headers-only identity, `Idempotency-Key` |
| `list_unextracted_entities(...)` | `db/ingest_repo.py` | selects eligible artifact nodes |
| `record_extraction_job(...)` | `db/ingest_repo.py` | writes the idempotency + cost ledger row |
| `supersede_extracted_edges(...)` | `db/graph_repo.py` | bitemporal close of the prior extracted slice |
| `upsert_edge(...)` / `upsert_entity(...)` / `get_entity_id(...)` | `db/graph_repo.py` | edge + target-node writes |
| `cypherx_a1.edges` | `db/migrations/20260614_0001__init.sql` | `confidence`, `extractor_version`, `metadata`, `evidence_chunk_ids`, bitemporal `valid_to` |
| `cypherx_a1.extraction_jobs` | `db/migrations/20260614_0001__init.sql` | PK `(tenant_id, node_id, content_sha, extractor_version)`, `llm_call_id`, `cost_usd` |
| `POST /v1/extract` → `ExtractResponse` | `api/connectors.py`, `models/api.py` | the synchronous trigger |
| `cypherxa1_extraction_jobs_total{outcome}` | `core/metrics.py` | per-node outcome counter |
