# SharedCore integration boundary

> cypherx-a1 is a **consuming app** (peer of `xAgent/ax-1`), not a SharedCore service. It reuses Auth, llms-gateway, Guardrails, RAG, Memory, and Tool-Registry **only** through their versioned `/v1` HTTP contracts, authenticates as the `cypherx-a1` Contract-12 service principal while forwarding the caller's agent JWT, carries **no identity in any request body**, and pushes **zero business logic** into SharedCore.

This document is the authoritative description of where cypherx-a1's process boundary sits: which SharedCore service is used for what, the exact call/identity pattern, what state stays inside the app, how the HTTP clients are implemented, and the fail-open vs fail-closed matrix that governs each downstream dependency.

All paths, function names, field names, and table/column names below are quoted verbatim from the code under `src/cypherx_a1/`. The client implementations live in `src/cypherx_a1/services/` (`service_token.py`, `llms_client.py`, `guardrails_client.py`, `rag_client.py`, `memory_client.py`); inbound JWT verification lives in `src/cypherx_a1/core/auth.py`; trace propagation in `src/cypherx_a1/core/trace.py`; the error envelope in `src/cypherx_a1/core/errors.py`.

---

## 1. The boundary in one sentence

cypherx-a1 owns its **tenant-scoped knowledge graph** (Postgres schema `cypherx_a1`: `entities`, `edges`, `identities`, `raw_events`, `citations`, `resource_acls`, `rag_kbs`, `outbox`, …) and its **app-side retrieval logic** (hybrid fusion / keyword / RRF / rerank / query-expansion); it **leases** vector search from RAG, **rents** conversational working memory from Memory, **calls** llms-gateway as the only path to a provider, **screens** copilot I/O through Guardrails, and **trusts** Auth for identity. Nothing it owns is replicated into SharedCore, and nothing SharedCore owns (cost metering, embedding generation, JWT signing, guardrail policy) is re-implemented in the app.

---

## 2. Per-service boundary table

| SharedCore service | Used for | Endpoints called | Call pattern | What STAYS in cypherx-a1 |
|---|---|---|---|---|
| **Auth** | (a) verify the inbound agent JWT locally against JWKS (defense in depth); (b) mint the `cypherx-a1` service token; (c) register agents/keys (ops). | `GET /.well-known/jwks.json` (verify); `POST /v1/service-tokens` (mint). | JWKS pull is local RS256 verification (`PyJWKClient`, 5-min key cache). Token mint uses `X-Service-Name` + `X-Service-Bootstrap-Secret` headers, body `{ "on_behalf_of": "<agent_id>" }`. | **Per-repo / per-team resource ACLs** live in app table `resource_acls`. Auth does coarse agent identity + scopes; cypherx-a1 does fine-grained engineering-resource authorization. App never stores signing keys. |
| **llms-gateway** | The **ONLY** path to a provider: (a) knowledge-extraction chat (`response_format={"type":"json_object"}`); (b) copilot answer generation; (c) ad-hoc query-time embeddings (rare — corpus embeddings go via RAG). | `POST /v1/chat/completions`; `POST /v1/embeddings`. | Service JWT in `Authorization`, agent JWT in `X-Forwarded-Agent-JWT`, W3C trace, optional `Idempotency-Key`. Body carries `model`, `messages`, `max_tokens`, `temperature`, `stream:false`, optional `response_format`. | Prompt construction, model alias selection, JSON-extraction parsing/repair, retrieval context. **Cost is never rewritten**: `usage.cost_usd` + `llm_call_id` from the gateway are the billing truth. |
| **RAG** | The **dense vector/semantic corpus** — one leg of hybrid retrieval. Per-tenant KBs `eng-code` / `eng-conversations` / `eng-docs` / `eng-incidents`, each created with an **explicit pinned embedding model**. | `POST /v1/kbs` (create); `POST /v1/kbs/{kb_id}/documents` (inline ingest ≤100 KiB); `POST /v1/kbs/{kb_id}/query` (dense). | Headers as above (+ `Idempotency-Key` on ingest). Query body: `top_k` ≤ 100, `ef_search` ≤ 500, `search_mode:"dense"`, `min_score`, optional `@>`-containment `filters` only. | The **GRAPH never enters RAG** (`rag.chunks` are opaque text + JSONB). Hybrid fusion, keyword search, RRF, rerank, query expansion, and range/time filtering are **all app-side**. KB→logical-name mapping persisted in `rag_kbs` (with the resolved model). |
| **Memory** | Copilot **conversational working memory ONLY** — per-principal episodic context across chat turns (prior Q/A, session continuity). | `POST /v1/sessions` (ensure); `POST /v1/memories/search` (recall); `POST /v1/memories` (store, `scope:"principal_only"`). | Headers as above (+ `Idempotency-Key` on store). | The **knowledge graph MUST NOT go here** (cross-principal leakage + duplicated embedding cost). Memory holds only ephemeral conversation context; the durable corpus is the graph + RAG. |
| **Guardrails** | Pre/post screening of copilot question and answer. | `POST /v1/check/input` (`{text, task_id}`); `POST /v1/check/output` (`{text, input_text, task_id}`). | Headers as above. `input_text` on the output check lets Guardrails distinguish echoed-PII from model-introduced content. | No guardrail policy logic in-app. cypherx-a1 only **maps the decision**: `block` → `422 GUARDRAIL_VIOLATION`; `redact` → swap in `processed_text`; `allow`/`warn` → pass. |
| **Tool Registry + MCP** | Register the stateless `mcp-eng-memory@1.0.0` MCP server so AI coding agents can discover/query the engineering memory. | `POST /v1/tools` (register, ops-time) / discovery + `name@version` resolve. | Service principal + agent forwarding. | **Per-invocation tool metering is the CALLER's outbox** (xAgent's), never the tool's. `mcp-eng-memory` is stateless and emits no billing events. |

---

## 3. The identity model: dual-header, body-free

Identity flows on **headers only** (Contract 12 + Contract 13). No request body — inbound or outbound — ever carries `tenant_id`, `agent_id`, `trace_id`, or any other identity/reserved key. This is enforced symmetrically: inbound bodies are rejected if they carry such keys, and outbound bodies are built without them.

### 3.1 Inbound: verifying the agent JWT (`core/auth.py`)

cypherx-a1 is edge-facing: the frontend BFF / edge (or an api-key-exchanged JWT) submits a **bare agent JWT** in `Authorization: Bearer …`. The app **re-verifies it locally** (same posture as xAgent/llms/guardrails/rag), never trusting an upstream verification.

`_decode(token, settings)` enforces:

- signature via JWKS, algorithm `RS256` (a fixed allowlist — no `alg` confusion);
- `iss == auth_issuer_url`, `aud` contains `auth_platform_audience`;
- `exp`/`iss`/`aud`/`sub` **required** (`options={"require": [...]}`), `±60s` skew (`_CLOCK_SKEW_SECONDS`);

then `_resolve_principal` reads `tenant_id` and `agent_id` **only from the verified claims** (never the body) into a `Principal`:

```
Principal(tenant_id, agent_id, scopes, principal_type, api_key_id, raw_token, raw_claims, kid)
```

The caller must hold **at least one** allowed scope (else `403 FORBIDDEN`). The base allow-set is:

```
cypherxa1:query | cypherxa1:ingest | cypherxa1:admin | agent:execute | agent:admin | platform:admin
```

Per-endpoint scope gating tightens this: `query_scopes()` ⊇ `ingest_scopes()` ⊇ `admin_scopes()`, applied via `require_scope(principal, scopes, action)`.

The verified raw bearer is preserved on **`Principal.raw_token`** — this is the exact string forwarded downstream as `X-Forwarded-Agent-JWT`.

After signature/claims pass, the token runs through the shared Valkey **revocation MIRROR** (`_enforce_revocation`) keyed by `jti`/`kid`/`agent_id`/`iat`. **This check FAILS OPEN** (availability wins): if `revocation_check_enabled` is off, Valkey is absent, or the lookup errors/times out (`revocation_valkey_timeout_seconds`, default `0.15s`), the request proceeds and a `revocation_check_skipped_total` metric increments. A positive hit raises `TOKEN_REVOKED` (`401`).

`require_principal(request)` is the FastAPI dependency that ties verify + revocation together.

### 3.2 Outbound: the dual-header pattern

Every downstream SharedCore call carries **two distinct identities** plus the trace context. Every client builds this in its own `_headers(...)` (the pattern is identical across `llms_client.py`, `guardrails_client.py`, `rag_client.py`, `memory_client.py`):

```python
service_jwt = await self._tokens.get_token(on_behalf_of=on_behalf_of)
headers = {
    "Authorization": f"Bearer {service_jwt}",        # Contract 12: WHO is calling (cypherx-a1)
    "X-Forwarded-Agent-JWT": agent_jwt,              # Contract 13: WHOM the call is for (the agent)
    **trace.propagation_headers(),                   # W3C traceparent + X-Request-ID (+ tracestate)
}
if idempotency_key:
    headers["Idempotency-Key"] = idempotency_key     # llms chat / rag ingest / memory store
```

| Header | Identity | Source | Verified downstream as |
|---|---|---|---|
| `Authorization: Bearer <service-jwt>` | The **service** principal `cypherx-a1` | minted from Auth `POST /v1/service-tokens`, cached in-process | "is `cypherx-a1` allowed to call me, for this scope?" (service ACL) |
| `X-Forwarded-Agent-JWT: <agent-jwt>` | The **agent** the call serves | `Principal.raw_token`, forwarded verbatim | re-verified against JWKS; its `agent_id` must match the service token's `on_behalf_of` |
| `traceparent` / `X-Request-ID` / `tracestate` | n/a (correlation) | `trace.propagation_headers()` | distributed trace stitching (Contract 8) |
| `Idempotency-Key` | n/a (dedup) | caller-supplied | replay the prior result instead of re-spending |

`propagation_headers()` rebuilds the **same** trace for every hop from the per-request contextvars bound by `TraceContextMiddleware` (`traceparent` reconstructed via `current_traceparent()` from `trace_id_var`/`span_id_var`; `tracestate` re-emitted only after `sanitize_tracestate` validates it).

### 3.3 Why both headers, and the `on_behalf_of` binding

The service token answers *"who is allowed to call this service"*; the forwarded agent JWT answers *"on whose authority / for which tenant"*. The two are **cross-checked**: `ServiceTokenProvider._mint` sends body `{ "on_behalf_of": "<agent_id>" }` to Auth, and the downstream service verifies the minted service token's `on_behalf_of` matches the forwarded agent JWT's `agent_id`. This prevents a service principal from acting for an arbitrary agent. The token cache (`ServiceTokenProvider._cache`) is therefore keyed **per `on_behalf_of`**, one `_CachedToken{token, expires_at}` each, refreshed `_REFRESH_SKEW_SECONDS` (30s) before its `expires_in`.

---

## 4. Reserved-key and reserved-claim handling

Two distinct anti-spoof concerns sit on the boundary.

### 4.1 Reserved metadata keys in request bodies (Contract 13 anti-spoof)

All public request models in `src/cypherx_a1/models/api.py` set Pydantic `extra="forbid"`. Any inbound body that smuggles `tenant_id`, `agent_id`, `trace_id`, or another identity/reserved key is rejected `422`. The error module (`core/errors.py`) reserves the spelling: a rejected reserved metadata key is surfaced as **`VALIDATION_ERROR`** with `details.reason="RESERVED_METADATA_KEY"` (the `RESERVED_METADATA_KEY` constant is the documented Contract-13 guard). Identity therefore comes **only** from the JWT — never the body — on both ingest and query.

This guard is also why outbound bodies are minimal: e.g. the RAG query body is `{query, top_k, min_score, search_mode, ef_search, filters?}` and the llms chat body is `{model, messages, max_tokens, temperature, stream, response_format?}` — neither contains identity.

### 4.2 Reserved JWT claims: accept-but-ignore (Phase-13 forward-compat)

The agent JWT may carry forward-looking enterprise-hardening claims that cypherx-a1 does **not** act on at this phase. Per the phase-alignment guarantees, these are **accepted but ignored** — present in `Principal.raw_claims` (so they are forwarded verbatim in `X-Forwarded-Agent-JWT`) but never enforced by cypherx-a1:

| Reserved claim | Future purpose (Phase 13) | cypherx-a1 behavior now |
|---|---|---|
| `cnf` | proof-of-possession / sender-constrained tokens | ignored (forwarded verbatim) |
| `wkl_id` | workload identity | ignored |
| `behavior_policy_id` | behavioral policy binding | ignored |
| `delegation_*` | delegation chains | ignored |
| `approval_context` | human-in-the-loop approval | ignored |

The verifier validates only the standard claims (`iss`, `aud`, `exp`, `sub`, `tenant_id`, `agent_id`, `scopes`, plus optional `jti`/`iat`/`kid`/`api_key_id`). Unknown claims do not fail verification — forward-compatibility is mandatory.

---

## 5. Fail-open vs fail-closed matrix

Each dependency has a deliberate failure posture. **Safety controls fail closed; enrichment fails open.**

| Dependency | Posture | On 5xx / transport error | On a "deny" decision | Rationale |
|---|---|---|---|---|
| **Auth — JWKS verify** | **fail-closed** | `401 UNAUTHORIZED` ("Unable to verify token signing key.") | invalid signature/claims → `401` | identity is non-negotiable |
| **Auth — service-token mint** | **fail-closed** | `503 SERVICE_UNAVAILABLE` (cannot proceed without a service token) | `>=400` → `503` | no downstream call is possible without it |
| **Auth — revocation mirror** | **fail-OPEN** | check skipped, request proceeds; `revocation_check_skipped_total++` | positive hit → `401 TOKEN_REVOKED` | availability wins; the mirror is defense-in-depth, not the primary gate |
| **Guardrails (input/output)** | **fail-CLOSED** | `503 SERVICE_UNAVAILABLE` — **never silently "allow"** | a 2xx body with no/invalid `decision` ∉ {allow,warn,redact,block} also raises `503`; `decision="block"` → `422 GUARDRAIL_VIOLATION` | a guardrail is a safety control; absence of a valid verdict is a failure, not a pass |
| **llms-gateway (chat/embed)** | **fail-closed** | `503 SERVICE_UNAVAILABLE` | `>=400` → `503` | the answer/extraction cannot be produced without it |
| **RAG — create/ingest** | **fail-closed** | `503 SERVICE_UNAVAILABLE` (via `_post`) | `>=400` → `503` | ingest correctness matters; surfaces the failure |
| **RAG — query** | **fail-closed on errors, graceful on ACL deny** | `503 SERVICE_UNAVAILABLE` | **`403` → `RagQueryResult(forbidden=True)` (NOT raised)** so the orchestrator degrades to graph+keyword | a KB ACL deny must not 500 the whole answer; other non-2xx still raises |
| **Memory — search/store/session** | **fail-OPEN (best-effort)** | `search` returns `[]`; `store` returns `False`; `ensure_session` swallows | n/a (no hard deny path) | "availability over completeness" — a memory outage must NEVER fail a copilot answer |

### 5.1 Fail-closed in code — Guardrails

`GuardrailsClient._check` is explicit about treating a missing/invalid verdict as a failure, never a pass:

```python
decision = data.get("decision")
if decision not in ("allow", "warn", "redact", "block"):
    raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, "Guardrails returned no/invalid decision (failing closed).")
```

The copilot (`copilot/service.py`) then maps a `block` to the product error on both legs:

```python
if gi.decision == "block":   # PRE
    raise ApiError(ErrorCode.GUARDRAIL_VIOLATION, "Question blocked by input guardrails.")
...
if go.decision == "block":   # POST
    raise ApiError(ErrorCode.GUARDRAIL_VIOLATION, "Answer blocked by output guardrails.")
```

A `redact` swaps in `processed_text` (`question_eff` pre, `answer` post); the POST check is passed `input_text=question_eff` so the gateway can tell echoed-PII from model-introduced content.

### 5.2 Fail-open in code — Memory

`MemoryClient` degrades silently on every path:

```python
# search:  on >=400 or httpx.HTTPError → return []   (logs "memory_search_skipped")
# store:   on >=400 or httpx.HTTPError → return False (logs "memory_store_skipped")
# ensure_session: catches Exception → log "memory_ensure_session_skipped" (a 409 is non-fatal)
```

The copilot guards all memory use behind `copilot_memory_enabled` and treats an empty recall as simply "no prior context."

### 5.3 Graceful degradation in code — RAG ACL deny

`RagClient.query` distinguishes an authorization deny from a hard failure:

```python
if resp.status_code == 403:
    return RagQueryResult(kb_id=kb_id, results=[], forbidden=True)   # degrade, don't raise
if resp.status_code >= 400:
    raise ApiError(ErrorCode.SERVICE_UNAVAILABLE, f"RAG returned {resp.status_code}.")
```

This lets the retrieval orchestrator drop the dense leg for a forbidden KB while still answering from graph + keyword retrieval.

---

## 6. How the clients are implemented

All four downstream clients share one structure — a thin, typed wrapper over `httpx.AsyncClient` with no business logic.

### 6.1 Common shape

- **Construction**: `__init__(settings, token_provider, *, client=None)`. If no `httpx.AsyncClient` is injected, the client lazily creates one (`_http()`) with a per-service timeout (`{rag,llms,guardrails,memory}_timeout_seconds`) and owns its lifecycle (`_owns_client` → `aclose()`). Injection makes them trivially testable.
- **Identity**: every request goes through `_headers(...)`, which calls `ServiceTokenProvider.get_token(on_behalf_of=…)` and assembles the dual-header + trace set (§3.2). No client touches signing keys or constructs identity from a body.
- **Errors**: non-2xx and `httpx.HTTPError` map to the Contract-2 `ApiError` envelope via `core/errors.py` (rendered with `code`, `message`, `request_id`, `trace_id`, `timestamp`).
- **Metrics**: every call increments `metrics.downstream_calls_total.labels("<service>", "<outcome>")` with outcomes `ok` / `rejected` / `forbidden` / `error`.
- **Typed results**: responses are parsed into small dataclasses — `ChatCompletion`/`Usage` (llms), `GuardrailResult` (guardrails), `KbInfo`/`IngestResult`/`RagHit`/`RagQueryResult` (rag), `MemoryItem` (memory) — so callers never touch raw JSON.

### 6.2 `ServiceTokenProvider` (`service_token.py`)

Mints + caches the `cypherx-a1` Contract-12 service JWT. `_mint(on_behalf_of)` POSTs to `{auth_service_url}/v1/service-tokens` with headers `X-Service-Name: cypherx-a1` (`service_principal_name`) + `X-Service-Bootstrap-Secret: <service_bootstrap_secret>` and body `{ "on_behalf_of": "<agent_id> "}`. It reads `access_token` (or `token`) and `expires_in` (default `_DEFAULT_TTL_SECONDS = 300`), caching `_CachedToken{token, expires_at}` per `on_behalf_of` and refreshing `_REFRESH_SKEW_SECONDS = 30` early. A failed mint raises `503`. This is a verbatim port of xAgent ax-1's provider.

### 6.3 `LlmsClient` (`llms_client.py`)

- `chat(model, messages, max_tokens, temperature=0.7, response_format=None, …, idempotency_key=None)` → `POST /v1/chat/completions`, `stream:false`. Knowledge extraction passes `response_format={"type":"json_object"}`; the copilot passes none. `_parse_chat` extracts `choices[0].message.content`, `finish_reason`, `model`, `usage` (incl. `cost_usd`), and `llm_call_id` (falling back to `id`). **The gateway's `cost_usd` and `llm_call_id` are the billing key and are never rewritten.**
- `embed(model, inputs, …)` → `POST /v1/embeddings`. Note: corpus embeddings go **indirectly through RAG** (single embedding-cost owner); this direct path is only for rare ad-hoc query-time embeddings.

### 6.4 `RagClient` (`rag_client.py`)

- `create_kb(name, …)` → `POST /v1/kbs` with `embedding_model_alias = settings.rag_embedding_model` (the **explicit pinned model**, e.g. `text-embedding-3-small`, `rag_embedding_dim=1536`) — **never the repointable `embed` alias** — plus `chunking_strategy:"sentence"`, `private:false`. Returns `KbInfo(kb_id, embedding_model_resolved, embedding_dim)`; the resolved literal is persisted in app table `rag_kbs`.
- `ingest_inline(kb_id, name, content, source_type, metadata, …, idempotency_key)` → `POST /v1/kbs/{kb_id}/documents`. `name` truncated to 500 chars; `source_type` clamped to `markdown`/`text`. Inline only (≤100 KiB).
- `query(kb_id, query, top_k, …, filters)` → `POST /v1/kbs/{kb_id}/query` with `top_k = min(top_k, 100)`, `ef_search = min(rag_query_ef_search, 500)`, `search_mode:"dense"`, `min_score`. `filters` are `@>`-containment only (range/time filtering is app-side). `_parse_query` maps each hit to `RagHit(chunk_id, doc_id, content, score, metadata, source_name, source_uri)` — the provenance in `metadata` (e.g. `node_id`) is what lets the orchestrator map a hit back to a graph entity for a `citations` row.

RAG is **dense-only** first cycle; keyword, RRF fusion, rerank, and query-expansion all live in `src/cypherx_a1/retrieval/`.

### 6.5 `GuardrailsClient` (`guardrails_client.py`)

- `check_input(text, task_id, …)` → `POST /v1/check/input`, body `{text, task_id}`.
- `check_output(text, input_text, task_id, …)` → `POST /v1/check/output`, body `{text, input_text, task_id}`.

Both return `GuardrailResult(decision, processed_text, violations)` and **fail closed** (§5.1). Direct port of the xAgent ax-1 guardrails client.

### 6.6 `MemoryClient` (`memory_client.py`)

- `ensure_session(session_id, …)` → `POST /v1/sessions` (best-effort; `409`/transport never raises).
- `search(query, top_k, …)` → `POST /v1/memories/search`, body `{query, top_k, include_shared:false}`; returns `list[MemoryItem]`, `[]` on any failure.
- `store(content, memory_type, session_id, …, idempotency_key)` → `POST /v1/memories`, body `{content, type, scope:"principal_only"}` (+ `session_id` if present); returns `bool`, `False` on failure.

`include_shared:false` and `scope:"principal_only"` keep memory strictly per-principal — the cross-principal-leakage guard that keeps the knowledge graph out of Memory.

---

## 7. Copilot flow across the boundary (end-to-end)

`CopilotService.ask(...)` (`copilot/service.py`) is the canonical sequence, calling llms-gateway + guardrails **directly** (with a clean seam to route through xAgent later):

```
1. memory recall          MemoryClient.search          (best-effort, fail-open)
2. PRE-guardrail           GuardrailsClient.check_input (fail-closed; block→422)
3. hybrid retrieve         RetrievalOrchestrator        (graph + RAG-dense + keyword, RRF, cited)
4. prompt build            app-side (system + memory + retrieved context)
5. LLM answer              LlmsClient.chat              (fail-closed)
6. POST-guardrail          GuardrailsClient.check_output(input_text=question_eff; block→422; redact→swap)
7. store episodic memory   MemoryClient.store           (best-effort; idempotency_key=f"{task_id}:mem")
→ AskResponse(answer, citations, used, trace_id, duration_ms)
```

Every step forwards the same `agent_jwt` (the verified `Principal.raw_token`) and `on_behalf_of=agent_id`, so the dual-header identity and the W3C trace are consistent across the whole fan-out. The `citations` returned with every answer come from the retrieval layer and map back to graph entities via the `citations` table (`doc_id → entity`), so an autonomous coding agent can verify each claim's source.

---

## 8. Invariants (do-not-violate)

1. **No business logic in SharedCore.** cypherx-a1 adapts to the `/v1` contracts with additive-field tolerance; it never asks a SharedCore service to learn about graphs, ACLs, or engineering semantics.
2. **Identity on headers, never bodies** — both directions. Inbound bodies `extra="forbid"`; reserved keys → `VALIDATION_ERROR` / `details.reason="RESERVED_METADATA_KEY"`. Outbound bodies contain no identity.
3. **The graph stays out of RAG and out of Memory.** RAG holds opaque chunks; Memory holds per-principal conversation only. The durable corpus is the app-owned graph (+ RAG dense index for retrieval).
4. **Never rewrite gateway cost.** `usage.cost_usd` + `llm_call_id` are the billing truth.
5. **Pin the embedding model explicitly** at KB creation — never the repointable `embed` alias.
6. **Safety fails closed; enrichment fails open.** Guardrails + Auth-verify + llms-call hard-fail; Memory + the revocation mirror degrade; a RAG `403` degrades the dense leg without failing the answer.
7. **Accept but ignore reserved JWT claims** (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) — forward-compat for Phase-13 hardening.
8. **Tool metering belongs to the caller's outbox** (xAgent), not the stateless `mcp-eng-memory` server.
