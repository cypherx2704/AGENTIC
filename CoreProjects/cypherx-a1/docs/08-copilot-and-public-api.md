# 08 — Copilot & public API

> The cited AI copilot flow and the full `/v1` REST surface of cypherx-a1: request/response shapes, the Contract-2 error envelope, the `extra="forbid"` reserved-key guard, the guardrail-decision → HTTP mapping, and the `cypherxa1:query/ingest/admin` scope model.

This document is the contract for the **public HTTP surface** of the cypherx-a1 product service (host `:8093`, in-container `:8080`). The `mcp-eng-memory` facade (host `:8094`) proxies the read-only `/v1/graph/*` query surface over the MCP contract and is covered in [09-mcp-server.md](09-mcp-server.md); this doc covers the REST API and the copilot engine that backs `POST /v1/copilot/ask`.

Authoritative source files:

| Concern | File |
| --- | --- |
| Copilot flow | `src/cypherx_a1/copilot/service.py` (`CopilotService.ask`) |
| Graph query logic | `src/cypherx_a1/copilot/queries.py` (`GraphQueryService`) |
| Wire models | `src/cypherx_a1/models/api.py` |
| Error envelope | `src/cypherx_a1/core/errors.py` (`ApiError`, `ErrorCode`) |
| Auth / scopes | `src/cypherx_a1/core/auth.py` (`require_principal`, `require_scope`) |
| Routers | `src/cypherx_a1/api/{copilot,graph,connectors,webhooks}.py` |
| Guardrails client | `src/cypherx_a1/services/guardrails_client.py` |
| Retrieval | `src/cypherx_a1/retrieval/orchestrator.py` |

---

## 1. The copilot flow

`POST /v1/copilot/ask` runs the **cited-answer** pipeline implemented in `CopilotService.ask`. The decided design (per the locked decisions) calls **llms-gateway and guardrails directly** — there is a clean seam to later route through xAgent, but for the MVP the product owns the orchestration. Memory is conversational working memory only; the knowledge graph never enters Memory.

The seven stages, in order, exactly as coded:

```
memory recall  →  PRE-guardrail(question)  →  hybrid retrieve  →  prompt build
              →   llms chat  →  POST-guardrail(answer, input_text=question)
              →   store episodic memory  →  cited answer
```

| # | Stage | What happens | Failure mode |
| --- | --- | --- | --- |
| 1 | **Memory recall** | If `copilot_memory_enabled`: `MemoryClient.ensure_session(session_id)` (when a `session_id` is supplied), then `MemoryClient.search(query=question, top_k=3)`. Prior turns are joined into a `memory_block`. | **Best-effort** — a Memory outage never fails an answer. |
| 2 | **PRE-guardrail** | `GuardrailsClient.check_input(question, task_id)` → `POST /v1/check/input`. `decision=block` → `422 GUARDRAIL_VIOLATION` (metric `copilot_requests_total{outcome="blocked_input"}`). `decision=redact` swaps `question_eff = processed_text`. | **Fail-closed** — any 4xx/5xx or missing/invalid decision raises `503 SERVICE_UNAVAILABLE`. |
| 3 | **Hybrid retrieve** | `RetrievalOrchestrator.retrieve(...)` fuses three legs (graph FTS + RAG-dense + keyword tsvector) with **RRF**, returns a token-bounded, fully-cited `RetrievalResult`. | RAG `403` per-KB is skipped (ACL); retrieval degrades, never throws on a forbidden KB. |
| 4 | **Prompt build** | System prompt + (optional) memory block + retrieved `context_text()` + `Question: <question_eff>`. Context is never truncated *away* — when empty, the user message says `(no matching context found)`. | — |
| 5 | **LLM answer** | `LlmsClient.chat(model=copilot_model, messages, max_tokens, temperature)` → `POST /v1/chat/completions`. The gateway is the **only** path to a provider; `llm_call_id` is the billing key and is never rewritten. | llms outage → `503` (mapped by the llms client). |
| 6 | **POST-guardrail** | `GuardrailsClient.check_output(answer, input_text=question_eff, task_id)` → `POST /v1/check/output`. `input_text` lets the screen distinguish *echoed* PII (present in the question) from *leaked* PII. `block` → `422`; `redact` → `answer = processed_text`. | **Fail-closed**, same as stage 2. |
| 7 | **Store episodic memory** | If `copilot_memory_enabled`: `MemoryClient.store(content="Q: …\nA: …"[:800], memory_type=copilot_memory_type, session_id, idempotency_key=f"{task_id}:mem")`. | **Best-effort** — never fails the answer. |

The handler returns an `AskResponse` with the (possibly redacted) `answer`, the retrieval `citations`, the `used` leg-count map, the `trace_id`, and `duration_ms`. A `task_id` (`uuid4`) is generated per request and threaded through every guardrail call and the memory idempotency key.

### 1.1 System prompt

`_SYSTEM_PROMPT` (verbatim in `service.py`) constrains the model to answer **only from the provided context**, be concise and concrete, name specific repos/PRs/services/people when supported, and say so plainly when the context is insufficient rather than guessing. This is what makes the copilot *grounded* — the citations let an autonomous agent verify the source.

### 1.2 Identity propagation

Every downstream SharedCore call carries identity in **headers only** (Contract 12/13):

- `Authorization: Bearer <service_jwt>` — minted by `ServiceTokenProvider.get_token(on_behalf_of=agent_id)`.
- `X-Forwarded-Agent-JWT: <agent_jwt>` — the verified inbound bearer, forwarded verbatim (`principal.raw_token`).
- W3C trace headers via `trace.propagation_headers()`.

The request **body carries no identity** — `tenant_id`/`agent_id` come only from the verified JWT. This is enforced both at the auth layer and by `extra="forbid"` on every request model (§5).

---

## 2. Endpoint catalogue

All product endpoints are mounted on the cypherx-a1 app. Identity (except the webhook receiver) is a bare agent JWT in `Authorization`, re-verified locally against the Auth JWKS.

| Method | Path | Router | Scope required | Auth |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/copilot/ask` | `api/copilot.py` | `query` | Agent JWT |
| `POST` | `/v1/graph/who-owns` | `api/graph.py` | `query` | Agent JWT |
| `POST` | `/v1/graph/what-breaks` | `api/graph.py` | `query` | Agent JWT |
| `POST` | `/v1/graph/experts` | `api/graph.py` | `query` | Agent JWT |
| `POST` | `/v1/graph/why-built` | `api/graph.py` | `query` | Agent JWT |
| `POST` | `/v1/graph/neighbors` | `api/graph.py` | `query` | Agent JWT |
| `POST` | `/v1/connectors/{kind}/sync` | `api/connectors.py` | `ingest` | Agent JWT |
| `POST` | `/v1/extract` | `api/connectors.py` | `ingest` | Agent JWT |
| `POST` | `/webhooks/{kind}?tenant=<uuid>` | `api/webhooks.py` | — (HMAC) | Source signature |
| `GET` | `/livez`, `/readyz`, `/metrics` | `api/health.py` | — | None (Contract 7) |

The `/v1/graph/*` router carries `prefix="/v1/graph"`; the copilot, connectors, and webhooks routers declare full paths.

---

## 3. Copilot endpoint

### `POST /v1/copilot/ask` — scope `cypherxa1:query`

**Request** — `AskRequest`:

| Field | Type | Constraints | Default |
| --- | --- | --- | --- |
| `question` | `str` | `min_length=1`, `max_length=4000`, **required** | — |
| `session_id` | `str \| null` | `max_length=128` | `null` |
| `top_k` | `int` | `ge=1`, `le=50` | `8` |

```json
{ "question": "Who owns the billing service and what breaks if I change it?",
  "session_id": "review-thread-42",
  "top_k": 8 }
```

**Response** — `AskResponse`:

| Field | Type | Notes |
| --- | --- | --- |
| `answer` | `str` | The grounded, possibly-redacted answer text. |
| `citations` | `list[Citation]` | Provenance for every retrieved item (§6). Answers are never uncited. |
| `used` | `dict[str, Any]` | Leg counts: `{"graph": n, "keyword": n, "rag": n}` from the orchestrator. |
| `trace_id` | `str \| null` | Contract 8 W3C trace id. |
| `duration_ms` | `int \| null` | End-to-end wall time. |

```json
{ "answer": "billing-service is owned by Priya Rao …",
  "citations": [ { "kind": "entity", "title": "billing-service",
                   "entity_kind": "service", "natural_key": "svc:billing", "source": "github" } ],
  "used": { "graph": 4, "keyword": 3, "rag": 6 },
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "duration_ms": 812 }
```

---

## 4. Graph query endpoints — `/v1/graph/*`

The `/v1/graph/*` endpoints are **read-only, cited, no-LLM** queries answered directly from the app-owned graph by `GraphQueryService`. They are deterministic and fast, and they are the **same backing logic the `mcp-eng-memory` server proxies** — so an autonomous coding agent over MCP and a human over REST get identical, source-cited answers. Every query runs read-only inside an `in_tenant` tx (RLS-scoped to the caller's `tenant_id`). All require scope `cypherxa1:query`.

| Endpoint | Request model | `GraphQueryService` method | Answers |
| --- | --- | --- | --- |
| `POST /v1/graph/who-owns` | `TargetRequest` | `who_owns` | Who owns this repo/service/feature/document. |
| `POST /v1/graph/what-breaks` | `WhatBreaksRequest` | `what_breaks_if_changed` | Impacted entities (+ owners) if `target` changes. |
| `POST /v1/graph/experts` | `TopicRequest` | `experts_on` | People with the most signal on a topic. |
| `POST /v1/graph/why-built` | `TopicRequest` | `why_built` | PRs/features/decisions/tickets/docs behind a feature. |
| `POST /v1/graph/neighbors` | `WhatBreaksRequest` | `neighbors` | Typed adjacency (both directions) around `target`. |

### 4.1 Request models

| Model | Fields |
| --- | --- |
| `TargetRequest` | `target: str` (`min_length=1`, `max_length=300`). |
| `TopicRequest` | `topic: str` (`min_length=1`, `max_length=300`). |
| `WhatBreaksRequest` | `target: str` (`min_length=1`, `max_length=300`); `max_hops: int` (`ge=1`, `le=6`, default `3`). |

`/v1/graph/neighbors` reuses `WhatBreaksRequest`; its `max_hops` is passed as the `hops` argument.

### 4.2 Response — `GraphAnswer`

| Field | Type | Notes |
| --- | --- | --- |
| `items` | `list[dict[str, Any]]` | Structured, per-query result rows (see below). |
| `citations` | `list[Citation]` | Entity-kind citations (`kind="entity"`) for the resolved target and every result row. |
| `trace_id` | `str \| null` | Contract 8 trace id. |

`GraphAnswer` carries **no LLM-generated prose** — only structured items and citations.

### 4.3 Per-query `items` shapes

The target is first resolved with `graph_repo.find_entities(query=target, kinds=…, limit=1)`. If nothing resolves, the endpoint returns `items=[]`, `citations=[]` (a `200` with an empty result — *not* a 404; absence of a match is a valid answer).

| Query | `items[]` keys |
| --- | --- |
| `who-owns` | `person`, `natural_key`, `relations`, `confidence` (float), `signal` (int). |
| `what-breaks` | `entity`, `kind`, `natural_key`, `depth` (int), `owners` (list of up to 3 names). |
| `experts` | `person`, `natural_key`, `relations`, `score` (float), `signal` (int). |
| `why-built` | `artifact`, `kind`, `natural_key`, `url`. |
| `neighbors` | `entity`, `kind`, `natural_key`, `rel`, `confidence` (float). |

`who-owns` resolves the target across kinds `["repo", "service", "feature", "document"]`; `what-breaks` across `["service", "repo", "feature", "document"]`; `why-built` searches `["pr", "feature", "decision", "ticket", "document"]`; `neighbors` resolves across all kinds. The keyless GitHub fixtures seed explicit `owns`/`depends_on` edges so `who-owns`/`what-breaks` work **without an LLM**; extraction enriches the graph when a real provider is configured.

```json
// POST /v1/graph/what-breaks  {"target": "svc:billing", "max_hops": 2}
{ "items": [
    { "entity": "checkout-service", "kind": "service", "natural_key": "svc:checkout",
      "depth": 1, "owners": ["Priya Rao"] } ],
  "citations": [
    { "kind": "entity", "title": "billing-service", "entity_kind": "service", "natural_key": "svc:billing" },
    { "kind": "entity", "title": "checkout-service", "entity_kind": "service", "natural_key": "svc:checkout" } ],
  "trace_id": "0af7651916cd43dd8448eb211c80319c" }
```

---

## 5. Connector / extraction endpoints

These trigger ingestion and the LLM knowledge-extraction pass. Both require scope `cypherxa1:ingest`. In production they are also driven by the webhook receiver + a scheduled worker, but the endpoints make the path explicit and testable. The Kafka worker (`worker/runner.py`) is a documented scale-out seam — the MVP drives ingestion/extraction synchronously through these authenticated endpoints.

### `POST /v1/connectors/{kind}/sync` — scope `cypherxa1:ingest`

Pulls from the source (bundled mock fixtures when `CONNECTOR_MODE=mock`; live GitHub when configured), normalizes records into the graph, and embeds documents into RAG. Resumable via the `sync_cursors` table (per `connector_id` + `stream`); idempotent via `raw_events` `content_sha` dedup. `{kind}` is validated against `supported_kinds()`; an unknown kind → `404 NOT_FOUND`.

**Request** — `SyncRequest`:

| Field | Type | Constraints | Default | Notes |
| --- | --- | --- | --- | --- |
| `repo` | `str \| null` | `max_length=140` | `null` | `owner/name` seed for a live pull; **ignored in mock mode**. |
| `mode` | `"full" \| "incremental"` | — | `"full"` | `incremental` uses the stored cursor; `full` backfills. |

**Response** — `SyncResponse`:

| Field | Type |
| --- | --- |
| `connector` | `str` (echoes `{kind}`) |
| `records_seen` | `int` |
| `records_new` | `int` |
| `nodes_upserted` | `int` |
| `edges_upserted` | `int` |
| `docs_ingested` | `int` |
| `skipped_duplicate` | `int` |
| `errors` | `int` |

### `POST /v1/extract` — scope `cypherxa1:ingest`

Runs the LLM knowledge-extraction pass (`run_extraction`) over not-yet-extracted artifacts. Idempotent (keyed in `extraction_jobs`) and cost-metered (the `extraction_jobs` cost ledger; `llm_call_id` is the billing key, never rewritten). Takes **no request body**.

**Response** — `ExtractResponse`:

| Field | Type |
| --- | --- |
| `nodes_seen` | `int` |
| `nodes_extracted` | `int` |
| `edges_added` | `int` |
| `failed` | `int` |

### `POST /webhooks/{kind}?tenant=<uuid>` — signature-authenticated

The app-owned webhook receiver (RAG has no push ingestion). It verifies the source HMAC signature, normalizes the delivery into canonical records, and lands + graph-normalizes them. **This path is graph-only**: a webhook carries no agent JWT to forward to RAG, so RAG embedding is **deferred** to an authenticated `/v1/connectors/{kind}/sync` or the worker.

- Tenant binding (MVP): the per-tenant webhook URL carries `?tenant=<uuid>`; the HMAC signature is the authenticator. There is **no platform JWT** on this path. Production hardens this to a per-install path token (Phase 3).
- `{kind}` unknown → `404 NOT_FOUND`. Missing `?tenant` → `422 VALIDATION_ERROR`. Bad signature → `401 UNAUTHORIZED`. Non-JSON body → `422 VALIDATION_ERROR`.
- Success → **HTTP `202`** with body `{"accepted": true, "event": "<event>", "records_new": n, "nodes_upserted": n, "note": "RAG embedding deferred (no agent JWT on webhook path)"}`. The event type is read from `X-GitHub-Event` / `X-Event-Key` / `X-Event` (first present, else `"unknown"`).

---

## 6. The `Citation` provenance unit

Every answer (copilot) and graph-query result carries `Citation` objects so a consumer can verify the source. `Citation` is a response model (`extra="ignore"`).

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | `"entity" \| "chunk"` | Graph entity vs. a RAG knowledge chunk. |
| `title` | `str` | Human label (entity title / natural key / chunk source name). |
| `source` | `str \| null` | e.g. `github`, `rag`. |
| `uri` | `str \| null` | Source URL when known (entity `attrs.url` or RAG `source_uri`). |
| `entity_id` | `str \| null` | Graph entity id. |
| `entity_kind` | `str \| null` | Person / Service / Repo / Feature / Decision / Incident / PR / Ticket / Document. |
| `natural_key` | `str \| null` | Stable cross-source key. |
| `doc_id` | `str \| null` | RAG document id (links a chunk back to its entity). |
| `chunk_id` | `str \| null` | RAG chunk id. |
| `score` | `float \| null` | Best dense similarity score (chunks). |
| `snippet` | `str \| null` | Up to 240 chars of evidence text. |

In hybrid retrieval, a RAG chunk whose `doc_id` maps back to a graph entity **reinforces** that entity (the chunk and entity share a single fused citation) — this back-link is the value of hybrid retrieval. Items with no entity mapping surface as standalone `chunk:` citations.

---

## 7. Authentication & scopes

cypherx-a1 is an **edge-facing app**: callers (the frontend BFF/edge, or an api-key-exchanged JWT) submit a **bare agent JWT** in `Authorization`. The service re-verifies it locally against the Auth JWKS (defense-in-depth, same posture as xAgent/llms/guardrails/rag). `require_principal` is the FastAPI dependency that produces a `Principal`.

**Verification** (`_decode`): RS256 via JWKS, `iss == auth_issuer_url`, `aud` contains `auth_platform_audience`, `exp` valid (±60s skew via `_CLOCK_SKEW_SECONDS`), and `sub`/`iss`/`aud`/`exp` required. `tenant_id` must be present (else `401`). After signature/claims pass, the token runs through the shared **Valkey revocation mirror** (`_enforce_revocation`) which **fails open** (availability wins) and raises `401 TOKEN_REVOKED` on a hit.

### 7.1 Scope model

Three product scopes, defined in `core/auth.py`:

| Scope | Constant | Grants |
| --- | --- | --- |
| `cypherxa1:query` | `SCOPE_QUERY` | Copilot + all `/v1/graph/*` reads. |
| `cypherxa1:ingest` | `SCOPE_INGEST` | `/v1/connectors/{kind}/sync`, `/v1/extract`. |
| `cypherxa1:admin` | `SCOPE_ADMIN` | Admin operations; **implies** ingest + query (admin scopes are in every gate set). |

Platform scopes `agent:execute`, `agent:admin`, `platform:admin` are also admitted so a platform/admin JWT is not rejected at the dependency before the endpoint's finer check runs.

**Two-tier gating.** `require_principal` first enforces that the token holds **at least one** of `_BASE_ALLOWED_SCOPES` (`query`, `ingest`, `admin`, `agent:execute`, `agent:admin`, `platform:admin`) — else `403`. Then each handler calls `require_scope(principal, <set>, action)` for the finer check. The helper sets are **hierarchical**:

```
query_scopes()  = {query}  ∪ ingest_scopes()
ingest_scopes() = {ingest} ∪ admin_scopes()
admin_scopes()  = {admin, agent:admin, platform:admin}
```

So `ingest` (and `admin`) can run query endpoints, and `admin` can run ingest endpoints — but a pure `query` token **cannot** trigger sync/extract (it gets `403 FORBIDDEN` from `require_scope`).

A scope claim is read from `scopes` and accepts either a space-delimited string or a JSON array (`_scopes_of`).

### 7.2 Reserved JWT claims (accept-but-ignore)

Reserved Phase-13 claims (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) are **accepted but ignored** — logic is never gated on their presence or absence. They are preserved on `Principal.raw_claims` for forward compatibility.

---

## 8. The `extra="forbid"` reserved-key guard

Every request model derives from `_Req`, which sets `model_config = ConfigDict(extra="forbid")`. This is the **anti-spoof guard** (Contract 13): identity and reserved keys must come from the verified JWT, never the body. If a caller smuggles `tenant_id`, `agent_id`, `trace_id`, or any other unexpected field into a request body, pydantic rejects it and the validation handler returns:

```json
{ "error": { "code": "VALIDATION_ERROR",
             "message": "Request validation failed.",
             "details": { "errors": [ … pydantic error list … ] },
             "request_id": "…", "trace_id": "…", "timestamp": "…Z" } }
```

Response models use `extra="ignore"` (`_Resp`) — additive fields on downstream responses are tolerated, never hard-coded. (Note: a *reserved metadata key* found deeper in a payload surfaces as `VALIDATION_ERROR` with `details.reason="RESERVED_METADATA_KEY"`.)

---

## 9. Contract-2 error envelope

Every error renders through `core/errors.py` to the canonical Contract-2 shape:

```json
{ "error": {
    "code": "GUARDRAIL_VIOLATION",
    "message": "Question blocked by input guardrails.",
    "details": { "…": "optional" },
    "request_id": "…",
    "trace_id": "…",
    "timestamp": "2026-06-14T12:00:00.000Z" } }
```

`request_id`/`trace_id` come from the trace context-vars; `timestamp` is RFC-3339 UTC with a `Z` suffix. `details` is omitted when empty.

### 9.1 Error codes and default HTTP status

`ErrorCode` (SCREAMING_SNAKE_CASE) with `_DEFAULT_STATUS`:

| Code | HTTP | When |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | Bad/forbidden body field; missing `?tenant`; non-JSON webhook. |
| `UNAUTHORIZED` | 401 | Missing/malformed bearer; bad signature; invalid/expired token; missing `tenant_id` claim. |
| `TOKEN_REVOKED` | 401 | Token present in the Valkey revocation mirror. |
| `FORBIDDEN` | 403 | Missing required scope. |
| `NOT_FOUND` | 404 | Unknown connector `{kind}`. |
| `CONFLICT` | 409 | State conflict. |
| `GUARDRAIL_VIOLATION` | **422** | Copilot input or output blocked by guardrails (product domain code). |
| `BUDGET_EXCEEDED` | 402 | Budget cap hit. |
| `RATE_LIMIT_EXCEEDED` | 429 | Rate limit. |
| `SERVICE_UNAVAILABLE` | 503 | Downstream (guardrails/llms/rag/memory) unavailable, or guardrails returned no/invalid decision (fail-closed). |
| `INTERNAL_ERROR` | 500 | Unhandled exception. |

### 9.2 Handlers (`install_exception_handlers`)

| Handler | Renders |
| --- | --- |
| `ApiError` | The chosen code + `status_code`; logs at `error` for ≥500, else `info`. |
| `RequestValidationError` | `VALIDATION_ERROR` / 422 with `details.errors = exc.errors()` (this is where `extra="forbid"` violations land). |
| `StarletteHTTPException` | Maps status → code (401→`UNAUTHORIZED`, 403→`FORBIDDEN`, 404→`NOT_FOUND`, 409→`CONFLICT`, 429→`RATE_LIMIT_EXCEEDED`, 503→`SERVICE_UNAVAILABLE`; else `VALIDATION_ERROR`/`INTERNAL_ERROR`). |
| `Exception` (catch-all) | `INTERNAL_ERROR` / 500, logs `unhandled_exception` with `exc_info`. Never leaks internals to the client. |

---

## 10. Guardrail decision → HTTP mapping

Guardrails are a **safety control and fail closed**. `GuardrailsClient._check` treats any transport error, any `>= 400` status, or a `2xx` body whose `decision` is not one of `allow|warn|redact|block` as a hard failure → `503 SERVICE_UNAVAILABLE`. It **never silently allows**. A `GuardrailResult` carries `decision`, `processed_text`, and `violations`.

| Guardrail `decision` | Copilot behaviour | HTTP outcome |
| --- | --- | --- |
| `allow` | Proceed unchanged. | continues |
| `warn` | Proceed; the warning is advisory (logged, not blocking). | continues |
| `redact` | Swap in `processed_text` — for input → `question_eff`; for output → the redacted `answer`. | continues (transformed) |
| `block` | Raise `ApiError(GUARDRAIL_VIOLATION)`. | **422 `GUARDRAIL_VIOLATION`** |
| *(missing/invalid)* / 4xx / 5xx / transport error | Raise (fail-closed). | **503 `SERVICE_UNAVAILABLE`** |

Pre-stage `block` is metered `copilot_requests_total{outcome="blocked_input"}`; post-stage `block` is `{outcome="blocked_output"}`; success is `{outcome="ok"}`. Identity on every guardrails call is headers-only: service JWT + `X-Forwarded-Agent-JWT` + W3C trace; the body is `{"text": …, "task_id": …}` for input and `{"text": …, "input_text": …, "task_id": …}` for output — **no identity in the body**.

---

## 11. End-to-end examples

### 11.1 Copilot ask (happy path)

```http
POST /v1/copilot/ask HTTP/1.1
Authorization: Bearer <agent-jwt with cypherxa1:query>
traceparent: 00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01
Content-Type: application/json

{ "question": "Why was the retry queue introduced in the payments path?", "top_k": 6 }
```
→ `200` `AskResponse` with grounded `answer` + `citations` + `used` + `trace_id` + `duration_ms`.

### 11.2 Input blocked by guardrails

→ `422`
```json
{ "error": { "code": "GUARDRAIL_VIOLATION",
             "message": "Question blocked by input guardrails.",
             "request_id": "…", "trace_id": "…", "timestamp": "…Z" } }
```

### 11.3 Reserved-key spoof rejected

```json
{ "question": "…", "tenant_id": "attacker-supplied" }
```
→ `422 VALIDATION_ERROR` (`details.errors` lists the forbidden `tenant_id` field; identity is taken only from the JWT).

### 11.4 Sync without ingest scope

`POST /v1/connectors/github/sync` with a `cypherxa1:query`-only token → `403 FORBIDDEN` ("Token missing a required scope for connector:sync.").

### 11.5 Webhook delivery

`POST /webhooks/github?tenant=<uuid>` with a valid `X-Hub-Signature-256` → `202` `{"accepted": true, "event": "push", "records_new": 3, "nodes_upserted": 5, "note": "RAG embedding deferred (no agent JWT on webhook path)"}`. RAG embedding is deferred to an authenticated sync/worker run.

---

## 12. Invariants this surface must keep

- **Identity from JWT only.** `tenant_id`/`agent_id` are never read from a body; `extra="forbid"` enforces it. Bodies on downstream calls carry no identity.
- **Guardrails fail closed**; `block` → `422 GUARDRAIL_VIOLATION`; any guardrail outage → `503`.
- **Memory is best-effort** and is **conversational only** — the knowledge graph never enters Memory or RAG.
- **llms-gateway is the only path to a provider**; never rewrite the gateway's `llm_call_id`/cost.
- **Answers are always cited** — copilot and graph queries both return `Citation` provenance.
- **RAG is consumed via the versioned `/v1` contract** with additive-field tolerance (`_Resp` is `extra="ignore"`); keyword/RRF/rerank/expansion stay app-side.
- **Scope hierarchy** `admin ⊇ ingest ⊇ query` is enforced via the `*_scopes()` helper sets; a query-only token cannot ingest.
