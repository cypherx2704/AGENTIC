# Ingestion & connector SPI

> How any engineering source becomes tenant-scoped graph + RAG corpus: one source-agnostic connector SPI, one canonical model, an app-owned webhook receiver (RAG has no push), resumable scheduled sync with cursors, cross-tool identity resolution, and `content_sha` idempotent landing — written once, GitHub-first.

This document is the authoritative reference for the **ingestion tier** of cypherx-a1 (Autonomous Engineering Memory). It covers the connector SPI contract, the canonical record model every connector normalizes to, the two acquisition paths (PULL backfill/sync, PUSH webhook), the landing/normalization/embedding pipeline, cross-tool identity resolution, idempotency, backfill chunking, and the per-connector implementation guide (GitHub now; Jira/Slack next).

Source of truth for the shapes below: `src/cypherx_a1/connectors/base.py`, `src/cypherx_a1/connectors/github.py`, `src/cypherx_a1/connectors/registry.py`, `src/cypherx_a1/models/canonical.py`, `src/cypherx_a1/ingestion/pipeline.py`, `src/cypherx_a1/ingestion/normalizer.py`, `src/cypherx_a1/db/ingest_repo.py`, `src/cypherx_a1/api/connectors.py`, `src/cypherx_a1/api/webhooks.py`, and `db/migrations/20260614_0001__init.sql`.

---

## 1. Where ingestion sits

cypherx-a1 is a **consuming app** (peer of `xAgent/ax-1`), not a SharedCore service. Ingestion is the front of the product pipeline:

```
source (GitHub, Jira, Slack …)
   │  PULL  full_sync / incremental_sync  (scheduled, resumable, cursors)
   │  PUSH  verify_signature → parse_webhook  (app-owned receiver, RAG has no push)
   ▼
connector  → canonical records  (nodes + edges + docs)
   ▼
ingestion pipeline
   1. LAND      raw_events (idempotent on content_sha)
   2. NORMALIZE entities + edges into the app-owned graph (one in_tenant tx)
   3. RAG INGEST docs → SharedCore RAG KB; record vector_ref + citation; emit outbox event
   ▼
graph (cypherx_a1 schema)  +  RAG corpus (SharedCore, dense vectors)
   ▼
LLM knowledge-extraction (separate pass) → hybrid retrieval → cited copilot → MCP
```

Two acquisition modes, **both app-owned** — RAG offers no push ingestion, so cypherx-a1 owns the webhook receiver, the scheduler, and all cursor state itself. The pipeline is written **once** against the SPI; adding a new market source is *one SPI subclass + one registry row*, with zero changes to normalization, storage, retrieval, or the copilot.

---

## 2. The connector SPI

`src/cypherx_a1/connectors/base.py` defines the entire contract. A connector is **stateless w.r.t. tenant** — identity (`tenant_id`) and cursors are passed in by the pipeline; the connector only knows how to talk to its source and how to normalize.

```python
class Connector(ABC):
    kind: str = "base"                      # matches connectors.kind + the registry key

    async def full_sync(self, *, stream, cursor) -> SyncBatch          # PULL backfill (resumable)
    async def incremental_sync(self, *, stream, cursor) -> SyncBatch   # PULL delta
    def verify_signature(self, *, headers, body: bytes) -> bool        # PUSH auth
    def parse_webhook(self, *, event, payload: dict) -> list[CanonicalRecord]  # PUSH normalize
    def streams(self) -> list[str]                                     # which streams to backfill
```

### 2.1 The six SPI responsibilities

| SPI member | Mode | Responsibility |
|------------|------|----------------|
| `streams()` | PULL | Return the list of source streams to backfill (e.g. `["pulls", "issues"]`). The sync endpoint iterates these. |
| `full_sync(stream, cursor)` | PULL | Resumable backfill of one `stream` from `cursor`. Returns a `SyncBatch`. |
| `incremental_sync(stream, cursor)` | PULL | Delta sync of one `stream` since `cursor`. May default to `full_sync` semantics. |
| `verify_signature(headers, body)` | PUSH | HMAC-verify a webhook delivery against the connector's shared secret over the **raw** body. Returns `bool`. |
| `parse_webhook(event, payload)` | PUSH | Normalize one inbound delivery into canonical records (may be empty for ignored events). |
| `to_canonical` *(conceptual)* | both | Turn one source record (a PR, issue, commit, message …) into a `CanonicalRecord`. In the GitHub connector this is realized by the private record-builders `_repo_record`, `_pr_record`, `_issue_record`, `_person` — the single place source shape maps to the unified model. Both `full_sync` and `parse_webhook` funnel through the same builders, so PULL and PUSH produce **identical** canonical output for the same object. |

> The SPI is intentionally minimal: `verify_signature` / `parse_webhook` are **sync** (pure CPU — HMAC + JSON shaping), while `full_sync` / `incremental_sync` are **async** (network I/O). A connector that only does PUSH still must implement the PULL methods (return an empty `done` batch); a PULL-only connector returns `False` from `verify_signature`.

### 2.2 `SyncBatch` — one bounded page

```python
@dataclass
class SyncBatch:
    records: list[CanonicalRecord] = []
    next_cursor: str | None = None   # resume token for the NEXT page; None = no more pages
    done: bool = True                # backfill complete for this stream
```

`next_cursor` is **opaque to the pipeline** — only the connector understands it. When `next_cursor` is non-null the sync endpoint persists it to `sync_cursors` and the next tick resumes from there (see §6, backfill chunking). `done=True` with `next_cursor=None` ends the stream.

### 2.3 The registry

`src/cypherx_a1/connectors/registry.py` resolves a `kind` string to its implementation:

```python
_REGISTRY: dict[str, type[Connector]] = {
    "github": GitHubConnector,
    # "jira": JiraConnector,      # Phase 3
    # "slack": SlackConnector,    # Phase 3
}
def supported_kinds() -> list[str]              # sorted(_REGISTRY)
def get_connector(kind, settings) -> Connector  # raises KeyError on unknown kind
```

`supported_kinds()` gates both the sync endpoint (`POST /v1/connectors/{kind}/sync`) and the webhook receiver (`POST /webhooks/{kind}`); an unknown `kind` is a `404 NOT_FOUND`. **Adding a source = one row here + one subclass.**

---

## 3. The canonical model

`src/cypherx_a1/models/canonical.py` is the unified shape every connector targets. A `to_canonical` mapping turns one source record into a `CanonicalRecord`: a set of graph **NODES**, typed **EDGES** between them, and optional RAG **DOCS** (text to embed).

### 3.1 Records, nodes, edges, docs

```python
@dataclass
class CanonicalRecord:
    source: str            # "github"
    record_type: str       # "pull_request" | "issue" | "repository" | "topology" | …
    external_id: str       # stable source id, e.g. "acme/payments#101"
    content_sha: str       # idempotency key for landing (see §7)
    nodes: list[CanonicalNode]
    edges: list[CanonicalEdge]
    docs:  list[RagDoc]
    raw_payload: dict      # optional, landed verbatim into raw_events.payload
```

```python
@dataclass
class CanonicalNode:
    kind: EntityKind       # person|service|repo|feature|decision|incident|pr|ticket|document
    source: str
    natural_key: str       # dedup key within (tenant, kind)
    title: str | None
    search_text: str | None
    external_id: str | None
    attrs: dict
    identity_handles: list[tuple[str, str]]   # (source, handle) — person cross-tool anchors
    # .ref → NodeRef(kind, natural_key)
```

```python
@dataclass
class CanonicalEdge:
    rel: EdgeRel           # owns|authored|reviewed|depends_on|caused|resolved|mentions|
                           # decided_in|deployed|expert_in|part_of
    src: NodeRef
    dst: NodeRef
    confidence: float = 1.0
    metadata: dict

@dataclass
class RagDoc:
    kb: KbName             # eng-code | eng-conversations | eng-docs | eng-incidents
    name: str
    content: str
    node: NodeRef          # the graph node this text provenance-links back to
    metadata: dict
    source_type: Literal["markdown", "text"] = "markdown"
```

### 3.2 `NodeRef` — wire edges before any DB UUID exists

```python
@dataclass(frozen=True)
class NodeRef:
    kind: EntityKind
    natural_key: str
```

This is the keystone of the model. A node is referenced by a **stable `(kind, natural_key)` pair**, so an edge can name two nodes *before either has a database UUID*. The normalizer resolves every `NodeRef` to a concrete `entity_id` at upsert time (§5). `natural_key` is the dedup key within `(tenant, kind)`:

| kind | `natural_key` example | Notes |
|------|----------------------|-------|
| `person` | `alice@acme.io` | **canonical email, lowercased** — the cross-tool identity anchor |
| `repo` | `acme/payments` | GitHub `full_name` |
| `pr` | `acme/payments#101` | `{full_name}#{number}` |
| `ticket` | `acme/payments#issue-5` | issues namespaced separately from PRs |
| `service` | `auth-service` | bare service name |

### 3.3 The locked vocabularies

The graph schema (`db/migrations/20260614_0001__init.sql`) pins the same enums as `CHECK` constraints, so the canonical `Literal`s and the database agree:

- **`EntityKind` / `entities_kind_enum`**: `person, service, repo, feature, decision, incident, pr, ticket, document`.
- **`EdgeRel` / `edges_rel_enum`**: `owns, authored, reviewed, depends_on, caused, resolved, mentions, decided_in, deployed, expert_in, part_of`.
- **`KbName`**: `eng-code, eng-conversations, eng-docs, eng-incidents` (logical names; resolved to a per-tenant `kb_id` at first use — see §8).

> **The graph never enters RAG.** RAG holds only opaque doc text + provenance metadata. Nodes/edges live exclusively in the `cypherx_a1` schema; the only crossover is `entities.vector_ref = {kb_id, doc_id}` and the `citations` row that links a RAG doc back to its node.

---

## 4. The ingestion pipeline

`src/cypherx_a1/ingestion/pipeline.py` — `ingest_records()` and `_ingest_one()`. For each canonical record, three stages run:

| Stage | What happens | Transaction | Idempotency |
|-------|--------------|-------------|-------------|
| **1. LAND** | `ingest_repo.record_raw_event` inserts into `raw_events`. Duplicate → short-circuit the whole record (no re-normalize, no re-embed). | inside `_land_and_normalize` (one `in_tenant` tx with stage 2) | `ON CONFLICT (tenant_id, source, external_id, content_sha) DO NOTHING` |
| **2. NORMALIZE** | `normalizer.upsert_graph` upserts nodes → `entity_id`, records person identities, wires edges, captures the `NodeRef → entity_id` map. | same tx as stage 1 | partial unique index on the current slice (§5) |
| **3. RAG INGEST** | For each `RagDoc`: resolve the KB, `rag.ingest_inline` (HTTP, **outside any DB tx**), then a *second* tx records `vector_ref` + a citation + a `record.normalized` outbox event. | HTTP, then a separate `in_tenant` tx per doc | `idempotency_key = "{tenant}:{content_sha}:{kb}"` on the RAG call |

`IngestStats` is the return tally: `records_seen, records_new, nodes_upserted, edges_upserted, docs_ingested, skipped_duplicate, errors, sources`. **One bad record never aborts a backfill** — `ingest_records` catches per-record exceptions, increments `errors`, and continues.

### 4.1 The deferred-embedding split (critical)

`_ingest_one` has a hard branch:

```python
# 3) RAG ingest each doc … Skipped on the webhook path (no agent JWT to forward).
if rag is None or kb_resolver is None or not agent_jwt:
    return
```

When `rag` / `kb_resolver` / `agent_jwt` are absent, **only landing + graph normalization run** — docs are NOT embedded. This is exactly the webhook path (§4.2): a webhook carries no inbound agent JWT to forward to RAG (Contract-12 forwarding requires `X-Forwarded-Agent-JWT`), so RAG enrichment is **deferred** to a later authenticated sync or the worker. The graph is updated immediately; the vector corpus catches up on the next authenticated pass.

### 4.2 Two entry points, one pipeline

| Entry point | Auth | `agent_jwt` | RAG embed? | Source code |
|-------------|------|-------------|-----------|-------------|
| `POST /v1/connectors/{kind}/sync` | agent JWT (ingest scope) | `principal.raw_token` | **yes** | `api/connectors.py` |
| `POST /webhooks/{kind}?tenant=<uuid>` | HMAC signature | `None` | **no (deferred)** | `api/webhooks.py` |

Both call the *same* `ingest_records()`. The only difference is whether `agent_jwt` / `rag` / `kb_resolver` are supplied.

---

## 5. Normalization & graph upsert

`src/cypherx_a1/ingestion/normalizer.py` — `upsert_graph(conn, record)` runs on a connection already inside an `in_tenant` (RLS-scoped) transaction and returns a `GraphUpsert(node_ids, edges_upserted)`.

**Step 1 — nodes.** Each node is upserted to a stable `entity_id` via `graph_repo.upsert_entity`. Non-person nodes carry the record's `content_sha` (so the extraction pass can detect changes); person nodes pass `content_sha=None`.

**Step 2 — edges.** Each `CanonicalEdge` resolves its `src`/`dst` `NodeRef` to an `entity_id` via `_resolve_ref`:

1. Already in this record's `node_ids` map → use it.
2. Else `graph_repo.get_entity_id(kind, natural_key)` on the current slice → use it.
3. Else **stub-create** a minimal entity (`source="derived"`, `title=natural_key`) so the edge can *always* be wired. This is how an edge that names a service appearing only in an edge (e.g. `depends_on → payments-db`) still lands a node.

Edges are written with `extractor_version="ingest"` (distinguishing connector-derived edges from later LLM-extracted ones). The current-slice uniqueness is enforced by `uq_entities_natural_current` (`UNIQUE (tenant_id, kind, natural_key) WHERE valid_to IS NULL`) — bitemporal history is preserved by setting `valid_to` on superseded versions, never by deleting.

---

## 6. Scheduled sync, streams & cursors

### 6.1 The sync endpoint

`POST /v1/connectors/{kind}/sync` (`api/connectors.py`) is the explicit, testable PULL trigger. It requires `ingest_scopes()` = `{cypherxa1:ingest, cypherxa1:admin, agent:admin, platform:admin}`.

Request (`SyncRequest`, `extra="forbid"`):

```json
{ "repo": "acme/payments", "mode": "full" }   // mode: "full" | "incremental"; repo optional
```

Flow:

1. `get_or_create_connector` upserts a `connectors` row keyed `(tenant_id, kind, display_name)` where `display_name = body.repo or kind`; returns `connector_id`. `config` is merged (`config || EXCLUDED.config`) so repeated installs accumulate non-secret config.
2. For each `stream in connector.streams()`:
   - Read the resume position: `get_cursor(connector_id, stream)`, falling back to a **seed** (`body.repo` in live mode — GitHub's live sync passes `"owner/name"` as the cursor seed).
   - `mode=="incremental"` → `connector.incremental_sync(...)`, else `connector.full_sync(...)`.
   - Collect `batch.records`; if `batch.next_cursor` is set, `set_cursor(connector_id, stream, next_cursor)`.
3. Pass all collected records to `ingest_records(...)` with the caller's `raw_token` as `agent_jwt` (so RAG embed runs).

`SyncResponse` echoes the `IngestStats` counters.

### 6.2 Cursor state

`cypherx_a1.sync_cursors` is the resumable position, **one row per `(tenant_id, connector_id, stream)`**:

| column | type | meaning |
|--------|------|---------|
| `tenant_id` | UUID | RLS scope |
| `connector_id` | UUID | the install |
| `stream` | VARCHAR(60) | e.g. `repo:owner/name:pulls`, `pulls`, `issues` |
| `cursor` | TEXT | **opaque** connector cursor (page token, `since` timestamp, etc.) |
| `updated_at` | TIMESTAMPTZ | last advance |

`set_cursor` upserts with `ON CONFLICT … DO UPDATE`, so a sync that is interrupted resumes from the last persisted page on the next tick — the cursor is the at-least-once checkpoint.

### 6.3 Streams

`streams()` is connector-defined. GitHub:

```python
def streams(self):
    return ["fixtures"] if connector_mode == "mock" else ["pulls", "issues"]
```

The `stream` string flows verbatim into `sync_cursors.stream`, so a connector can use rich stream identifiers (e.g. `repo:acme/payments:pulls`) to keep per-repo cursors independent.

### 6.4 The scale-out worker

`src/cypherx_a1/worker/runner.py` (`CYPHERXA1_RUN_WORKER=1`) is the documented horizontal scale-out seam: a Redpanda consumer group over the `cypherx.cypherxa1.*` work topics (`raw.landed → record.normalized → extraction.requested → extraction.completed`), mirroring the rag-service worker split. **First-cycle status:** the MVP drives ingestion + extraction *synchronously through the authenticated API*; the worker currently logs and idles (a no-op heartbeat) and reuses the **same** `ingestion.pipeline` + `extraction.extractor` functions with a service-minted principal once wired (Phase 1.5). Scheduled/periodic sync is therefore "call the sync endpoint on a cron" today, and "consume work topics" once the worker is wired — both driving identical pipeline code.

---

## 7. `content_sha` idempotent landing

Every record carries a `content_sha`, computed by the connector over the *semantically meaningful* fields of the object. GitHub's `_sha(*parts)` is `sha256("\x1e".join(parts))`, e.g.:

```python
_sha("pr", pr_key, title, body, state)        # PR
_sha("issue", key, title, body)                # issue
_sha("repo", full_name, description)           # repo
```

Landing is a single idempotent insert (`ingest_repo.record_raw_event`):

```sql
INSERT INTO cypherx_a1.raw_events
    (tenant_id, source, external_id, record_type, content_sha, payload)
VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s, %s, %s)
ON CONFLICT (tenant_id, source, external_id, content_sha) DO NOTHING
```

`record_raw_event` returns `cur.rowcount > 0` → **True = newly landed, False = duplicate**. On a duplicate, `_ingest_one` increments `skipped_duplicate` and returns immediately — no graph write, no RAG call, no event. This is what makes **incremental sync cheap and safe**:

```python
async def incremental_sync(self, *, stream, cursor):
    # First cycle: incremental == a bounded full re-pull. content_sha dedup at the
    # landing stage skips unchanged objects, so re-pulling is cheap AND correct.
    return await self.full_sync(stream=stream, cursor=cursor)
```

A re-pull of 1,000 PRs where only 3 changed lands 3 new `raw_events` rows; the other 997 are no-ops. The unique key is `(tenant_id, source, external_id, content_sha)` — when an object *changes*, its `content_sha` changes, so it lands as a new row and re-normalizes (superseding the prior graph slice). The webhook path uses the same landing, so a PR opened *and* a webhook for the same PR converge on one landed row.

There is a **second** idempotency layer at the RAG boundary: `rag.ingest_inline(idempotency_key=f"{tenant_id}:{record.content_sha}:{doc.kb}")`, so even if a record re-lands (different `external_id`, same content), RAG de-dups the embed by content + KB.

---

## 8. KB resolution & RAG embedding

`KbResolver` (in `pipeline.py`) maps a logical KB name to a SharedCore RAG `kb_id`, **create-once per `(tenant, logical)`**, then in-process cached:

1. `ingest_repo.get_rag_kb(logical_name)` — already bound? return its `kb_id`.
2. Else `rag.create_kb(name=f"cypherx-a1::{logical}", …)` against RAG.
3. `ingest_repo.set_rag_kb(logical, kb_id, model, dim)` persists the binding **immutably** (`ON CONFLICT (tenant_id, logical_name) DO NOTHING` — first writer wins under a race), then re-reads the winner.

`cypherx_a1.rag_kbs` pins `embedding_model_resolved` + `embedding_dim` per `(tenant, logical_name)`. The model is the **explicit pinned** name (`rag_embedding_model = "text-embedding-3-small"`, dim 1536), never the repointable `embed` alias — every KB shares one stable vector space (the Phase-alignment guarantee). After ingest, `graph_repo.set_vector_ref` stamps `entities.vector_ref = {kb_id, doc_id}` and `add_citation` records the `doc_id → entity_id` provenance link, then a `cypherx.cypherxa1.record.normalized` event is enqueued to the outbox in the same tx.

---

## 9. Cross-tool identity resolution

One human appears as `alice` on GitHub, `@alice` on Slack, and `alice@acme.io` in Jira. cypherx-a1 collapses these into a **single canonical `person` entity** so `who_owns` / `experts_on` don't fragment.

**The anchor** is the person node's `natural_key` = canonical email (lowercased), plus `identity_handles: list[(source, handle)]`. GitHub emits:

```python
def _person(login, name, email):
    return CanonicalNode(kind="person", natural_key=email.lower(),
        identity_handles=[("github", login.lower()), ("email", email.lower())], …)
```

**The resolver** (`normalizer._resolve_person_by_handle` + `_record_identities`) runs *before* upserting any person:

1. For each `(source, handle)` of the incoming person, look it up in `cypherx_a1.identities`. If **any** handle already maps to a `person_entity_id`, reuse that entity — the human is not split across tools.
2. Upsert the person node to its own `entity_id`.
3. `canonical = existing or entity_id` — prefer the pre-existing canonical id; backfill this node's handles as aliases via:

```sql
INSERT INTO cypherx_a1.identities (tenant_id, person_entity_id, source, handle)
VALUES (NULLIF(current_setting('app.tenant_id', true), '')::uuid, %s, %s, %s)
ON CONFLICT (tenant_id, source, handle) DO NOTHING
```

`cypherx_a1.identities` is `UNIQUE (tenant_id, source, handle)` — a handle maps to exactly one canonical person per tenant. When the Slack connector lands, its `("slack", uid)` handle, paired with the same email (`("email", alice@acme.io)`), resolves to the GitHub-created person entity, and the Slack login is recorded as a new alias. **No merge job is needed** — resolution happens at ingest time.

---

## 10. The app-owned webhook receiver (PUSH)

`POST /webhooks/{kind}?tenant=<uuid>` (`api/webhooks.py`). **RAG has no push ingestion**, so cypherx-a1 owns this receiver. There is **no platform JWT on this path** — the HMAC signature *is* the authenticator.

Flow:

1. `kind` must be in `supported_kinds()` (else `404 NOT_FOUND`); `?tenant=<uuid>` is required (else `422 VALIDATION_ERROR`).
2. Read the **raw** body and lowercased headers.
3. `connector.verify_signature(headers, body)` — false → `401 UNAUTHORIZED`.
4. Determine the event from the first present header in `("x-github-event", "x-event-key", "x-event")`, defaulting to `"unknown"`.
5. `json.loads(body)` — invalid JSON → `422 VALIDATION_ERROR`.
6. `connector.parse_webhook(event, payload)` → canonical records.
7. `ingest_records(..., agent_jwt=None, rag=None, kb_resolver=None)` — **graph-only landing**; RAG embedding deferred (§4.1).
8. Respond `202 Accepted`:

```json
{ "accepted": true, "event": "pull_request", "records_new": 1,
  "nodes_upserted": 3, "note": "RAG embedding deferred (no agent JWT on webhook path)" }
```

**Tenant binding (MVP):** the per-tenant webhook URL carries `?tenant=<uuid>`; the HMAC signature authenticates the payload. Production hardens this to a per-connector-install **path token** (Phase 3). The receiver always `aclose()`s the connector's HTTP client in a `finally`.

### GitHub signature verification

`GitHubConnector.verify_signature` checks `X-Hub-Signature-256` (case-insensitive header), an HMAC-SHA256 of the **raw** body keyed by `github_webhook_secret`, compared with `hmac.compare_digest` (constant-time):

```python
expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
return hmac.compare_digest(expected, sig)
```

No secret configured → returns `False` (**fail-closed**). `parse_webhook` handles `pull_request` → `_pr_record`, `issues` → `_issue_record`, `push` → `_repo_record`; any other event is logged (`github_webhook_ignored_event`) and returns `[]`.

---

## 11. The GitHub connector (reference implementation)

`src/cypherx_a1/connectors/github.py`. `kind = "github"`. Two modes via `CONNECTOR_MODE`:

| `connector_mode` | `streams()` | `full_sync` | Use |
|------------------|-------------|-------------|-----|
| `mock` (default, keyless) | `["fixtures"]` | replays a bundled fixture repo (`_fixture_records()`) | the whole ingest→graph→RAG→copilot path runs end-to-end with **no GitHub token** |
| `live` | `["pulls", "issues"]` | calls the GitHub REST API (best-effort) | requires `GITHUB_TOKEN` + a `repo` seed |

### 11.1 Mock mode — the keyless demo spine

`_fixture_records()` ships a small, self-contained engineering history (`acme/payments`) with **explicit `depends_on`/`owns` edges** so the demo queries (`who_owns`, `what_breaks_if_changed`, `experts_on`, `why_built`) all work *without an LLM*. Knowledge **extraction** (the LLM pass) then enriches the graph with more edges when a real provider is configured. The fixtures emit:

- A `repository` record (`acme/payments`).
- A `topology` record: `service` nodes (`auth-service`, `payments-db`), persons, and edges `owns`, `depends_on` (`repo → auth-service`, `repo → payments-db`).
- Two `pull_request` records (#101, #102) with `authored` / `reviewed` / `part_of` edges and `eng-code` docs.
- One `issue` record (#5) with an `eng-docs` doc.

### 11.2 Live mode — best-effort REST pull

`_live_sync` requires `github_token` and a `repo` (`"owner/name"`, passed as the stream's cursor seed). Unconfigured → empty `done` batch (logs `github_live_sync_unconfigured`). It calls:

- `pulls` → `GET {github_api_url}/repos/{repo}/pulls?state=all&per_page={backfill_page_size}`
- `issues` → `GET {github_api_url}/repos/{repo}/issues?state=all&per_page={backfill_page_size}` (skips entries with a `pull_request` key — the issues endpoint also returns PRs)

with headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`. HTTP errors are caught (`github_live_sync_failed`) and the partial batch returned — one bad page never crashes a sync. **Backfill page size** is `backfill_page_size` (default 100), the bounded per-tick page; resumability is via `sync_cursors` once the connector returns `next_cursor` (the first-cycle live path returns `next_cursor=None`, i.e. single bounded page; richer pagination is the documented extension point).

### 11.3 The record-builders (`to_canonical` in practice)

| Builder | record_type | nodes | edges | docs |
|---------|-------------|-------|-------|------|
| `_repo_record` | `repository` | `repo` | — | — |
| `_pr_record` | `pull_request` | `pr`, author, reviewers | `authored` (author→pr), `part_of` (pr→repo), `reviewed` (reviewer→pr) | `eng-code` doc |
| `_issue_record` | `issue` | `ticket`, author | `authored`, `part_of` | `eng-docs` doc |
| `_person` | (node only) | `person` keyed by email | — | — |

Both `full_sync` (live) and `parse_webhook` call these same builders, guaranteeing PULL and PUSH produce identical canonical output.

### 11.4 GitHub connector settings

| Setting (env) | Default | Purpose |
|---------------|---------|---------|
| `connector_mode` | `mock` | `mock` = fixtures (keyless); `live` = GitHub API |
| `github_token` | `""` | live PAT/app token; empty → live sync is a no-op |
| `github_webhook_secret` | `local-dev-webhook-secret` | HMAC key for `X-Hub-Signature-256` |
| `github_api_url` | `https://api.github.com` | REST base (override for GHES) |
| `backfill_page_size` | `100` | `per_page` on live REST pulls |

---

## 12. Per-connector guide template (Jira / Slack next)

Adding a market source is a closed checklist. The pipeline, normalization, storage, retrieval, and copilot never change.

1. **Subclass `Connector`** in `src/cypherx_a1/connectors/<kind>.py`; set `kind = "<kind>"` (must match `connectors.kind` and the registry key).
2. **Implement `streams()`** — the source streams to backfill (Jira: `["issues"]` / per-project `project:<KEY>:issues`; Slack: `["messages"]` / per-channel `channel:<id>:messages`).
3. **Implement `full_sync` / `incremental_sync`** — return bounded `SyncBatch` pages; set `next_cursor` for the next page (Jira: `startAt`/JQL `updated >= <cursor>`; Slack: `cursor` from `conversations.history` / `latest` ts). Default `incremental_sync` to `full_sync` (content_sha dedup makes a re-pull cheap) until a true delta is available.
4. **Implement `verify_signature`** — Jira: shared-secret/JWT on the webhook; Slack: `X-Slack-Signature` (`v0=` HMAC-SHA256 of `v0:{ts}:{body}`) + `X-Slack-Request-Timestamp` replay window. Fail-closed when no secret is configured.
5. **Implement `parse_webhook`** — map source event types (Jira `jira:issue_created/updated`, `comment_created`; Slack `message`, `reaction_added`) to record-builders; return `[]` for ignored events.
6. **Write `to_canonical` record-builders** — pick the right `EntityKind`/`EdgeRel`/`KbName`. Suggested mappings:

   | Source object | entity kind(s) | edges | RAG KB |
   |---------------|----------------|-------|--------|
   | Jira issue / epic | `ticket` (+ `feature` for epics) | `authored`, `part_of`, `resolved`, `mentions` | `eng-docs` |
   | Jira incident ticket | `incident` | `caused`, `resolved`, `owns` | `eng-incidents` |
   | Slack message / thread | `document` (+ `decision` if a decision thread) | `mentions`, `decided_in` | `eng-conversations` |
   | Slack/Jira user | `person` keyed by **email** | — | — |

7. **Emit `identity_handles` on every person** — `[("slack", uid), ("email", e), …]` so cross-tool resolution (§9) collapses them onto the existing canonical person.
8. **Compute a stable `content_sha`** over the semantically-meaningful fields (the dedup boundary — change it only when re-processing should happen).
9. **Register it** — add one row to `_REGISTRY` in `registry.py` and uncomment the import.
10. **Seal credentials** in `connector_secrets` (`sealed:v1:<…>` or `env:<NAME>`), 1:1 with `connectors`; never put secrets in `connectors.config`.

That is the entire surface. The webhook receiver, sync endpoint, landing, normalization, identity resolution, KB resolution, citations, and outbox all work unchanged for the new `kind`.

---

## 13. Tenancy, RLS & invariants (ingestion view)

- **Every ingestion table is tenant-scoped with FORCE RLS** (`USING tenant_id = NULLIF(current_setting('app.tenant_id', true),'')::uuid`): `raw_events, connectors, connector_secrets, sync_cursors, identities, extraction_jobs, citations, rag_kbs` (plus `entities`/`edges`/`resource_acls`). The runtime role `cxa1_user` is **not** a superuser and does **not** `BYPASSRLS`. An unset `app.tenant_id` GUC yields zero rows (the NULLIF guard), never an error.
- **`outbox` has NO RLS** — it is an internal cross-tenant publish queue drained by a background task; isolation is in the Contract-5 payload (`partition_key = tenant_id`), not the row.
- **One tenant per org, shared graph** — `tenant_id` comes only from the verified JWT (sync path) or the `?tenant` binding behind an authenticated HMAC (webhook path), never from a request body (`SyncRequest` is `extra="forbid"`).
- **`raw_events` is append-only** (`cxa1_user` has `SELECT, INSERT` only) — immutable landing/audit.
- **Bitemporal, never destructive** — re-landing a changed object supersedes the prior graph slice (`valid_to`), keeping history.

---

## 14. Quick reference

| Concern | Where |
|---------|-------|
| SPI contract | `connectors/base.py` — `Connector`, `SyncBatch` |
| Canonical model | `models/canonical.py` — `CanonicalRecord/Node/Edge`, `RagDoc`, `NodeRef` |
| Registry | `connectors/registry.py` — `supported_kinds()`, `get_connector()` |
| GitHub connector | `connectors/github.py` — `GitHubConnector` |
| Pipeline (land→normalize→embed) | `ingestion/pipeline.py` — `ingest_records`, `_ingest_one`, `KbResolver` |
| Graph normalize + identity | `ingestion/normalizer.py` — `upsert_graph`, `_resolve_person_by_handle` |
| Landing / cursors / connectors DAO | `db/ingest_repo.py` |
| Sync trigger | `POST /v1/connectors/{kind}/sync` (`api/connectors.py`) |
| Webhook receiver | `POST /webhooks/{kind}?tenant=<uuid>` (`api/webhooks.py`) |
| Worker (scale-out seam) | `worker/runner.py` |
| Schema (tables, RLS, enums) | `db/migrations/20260614_0001__init.sql` |
| Service ports | cypherx-a1 host **8093** (in-container 8080); mcp-eng-memory host **8094** |
| Work topics | `cypherx.cypherxa1.*` (Contract-5, `partition_key=tenant_id`, paired `.dlq`); `record.normalized` emitted on embed |
