# MCP server design (mcp-eng-memory)

> `mcp-eng-memory` is a **stateless Contract-4 MCP facade** that exposes seven read-only, source-cited engineering-memory tools to AI coding agents by proxying to the `cypherx-a1` product API. It owns **no database, no Kafka, no outbox**: it verifies the inbound JWT (dual-mode), enforces a dual scope, validates `{tool, args}` against the committed manifest, forwards the resolved agent JWT to the backend, and returns a cited result. Per-invocation metering is the **calling agent's (xAgent) outbox**, never this server's.

This document is the authoritative design of the facade. Every path, function name, field name, scope string, and cap below is quoted verbatim from the code under `mcp-eng-memory/src/mcp_eng_memory/`. The package is a lean sibling of the main `cypherx_a1` package — no `psycopg`, no `aiokafka`, no DB-pool dependency — entrypoint `python -m mcp_eng_memory` (image port `8080`; compose host map **8094**).

Owning files:

| Concern | File |
|---|---|
| App factory + lifespan (no DB/Kafka) | `src/mcp_eng_memory/main.py` |
| Inbound JWT verify (dual-mode + dual scope) | `src/mcp_eng_memory/core/auth.py` |
| Settings (scopes, caps, backend URL, manifest path) | `src/mcp_eng_memory/core/config.py` |
| Contract-2 error envelope | `src/mcp_eng_memory/core/errors.py` |
| W3C trace middleware + propagation | `src/mcp_eng_memory/core/trace.py` |
| Prometheus metrics | `src/mcp_eng_memory/core/metrics.py` |
| `POST /mcp/v1/invoke` | `src/mcp_eng_memory/api/invoke.py` |
| `GET /manifest` (ETag/304) | `src/mcp_eng_memory/api/manifest.py` |
| `/livez` `/readyz` `/metrics` | `src/mcp_eng_memory/api/health.py` |
| Manifest load / ETag / input validation | `src/mcp_eng_memory/services/manifest.py` |
| Backend proxy client | `src/mcp_eng_memory/services/backend.py` |
| Committed manifest (source of truth) | `mcp-eng-memory/manifest.json` |

---

## 1. Why a separate facade exists

The MCP server is deliberately a **separate process and package** from the `cypherx-a1` product service, not a router mounted inside it. The split is load-bearing:

1. **Statelessness is enforced by construction.** The facade package has no DB or Kafka client in its dependency tree, so it *cannot* write state, cannot emit billing events, and cannot bypass tenant RLS. The invariant "the MCP server never meters" is impossible to violate because the code to meter is not present.
2. **Blast-radius isolation.** An AI coding agent (potentially external, potentially mid-deploy) hits the facade. If it is overrun, the product service — which owns the graph, the connectors, and the extraction ledger — is untouched.
3. **Contract-4 surface is narrow.** The facade speaks exactly one external contract (MCP manifest + invoke). All domain logic, retrieval fusion, guardrail screening, and cost truth live behind it in `cypherx-a1`, reached over the same `/v1` HTTP boundary that xAgent and the BFF use.

The facade is a **proxy with three jobs**: authenticate, validate, forward. It re-implements no business logic. Whatever the `cypherx-a1` backend returns (graph items, copilot answers, citations) is what it returns, capped and enveloped.

```
AI coding agent / external IDE / xAgent
        │  Authorization: Bearer <agent-or-service JWT>
        │  X-Forwarded-Agent-JWT: <agent JWT>   (internal mode only)
        │  traceparent / X-Request-ID
        ▼
┌─────────────────────────────┐
│  mcp-eng-memory  (:8080)    │  STATELESS — no DB, no Kafka, no outbox
│  - verify JWT (JWKS RS256)  │
│  - dual scope check         │
│  - body cap (1 MiB)         │
│  - manifest input validate  │
│  - dispatch → forward JWT   │
│  - output cap (10 MiB)      │
└──────────────┬──────────────┘
        │  Authorization: Bearer <resolved agent JWT>
        │  traceparent / X-Request-ID
        ▼
┌─────────────────────────────┐
│  cypherx-a1  (:8093)        │  re-verifies agent JWT (incl. revocation),
│  /v1/graph/*                │  sets app.tenant_id, enforces RLS, runs
│  /v1/copilot/ask            │  hybrid retrieval + guardrails + LLM,
└─────────────────────────────┘  meters its OWN usage on its own topic
```

---

## 2. The seven tools

All seven tools are **read-only, `idempotent: true`, and source-citing**. Five are pure graph queries (no LLM, deterministic, cheap); two are copilot questions that route through the full cited-answer flow (guardrails → hybrid retrieval → llms-gateway). The tool set, its input schema, the backend call it maps to, and its declared timeout ceiling are all fixed in `manifest.json`.

| Tool | Required args | Optional args | Backend call (`_dispatch`) | LLM? | `timeout_seconds` |
|---|---|---|---|---|---|
| `who_owns` | `target` (string, minLength 1) | — | `POST /v1/graph/who-owns` `{target}` | no | 20 |
| `why_built` | `feature` (string, minLength 1) | — | `POST /v1/graph/why-built` `{topic: args["feature"]}` | no | 20 |
| `what_breaks_if_changed` | `target` (string, minLength 1) | `max_hops` (int 1–6, default 3) | `POST /v1/graph/what-breaks` `{target, max_hops}` | no | 25 |
| `experts_on` | `topic` (string, minLength 1) | — | `POST /v1/graph/experts` `{topic}` | no | 20 |
| `graph_neighbors` | `target` (string, minLength 1) | `max_hops` (int 1–4, default 2) | `POST /v1/graph/neighbors` `{target, max_hops}` | no | 20 |
| `incident_root_cause` | `incident` (string, minLength 1) | — | `POST /v1/copilot/ask` (templated question) | yes | 60 |
| `how_does_x_work` | `topic` (string, minLength 1) | — | `POST /v1/copilot/ask` (templated question) | yes | 60 |

### 2.1 Tool semantics

- **`who_owns`** — owners/maintainers of a repo (`owner/name`), service, feature, or file path, with evidence. Walks the `owns` edges and `Person` entities in the graph.
- **`why_built`** — the PRs, decisions, RFCs, or tickets behind a feature/change. Note the wire mapping: the tool's `feature` argument is sent to the backend as `{"topic": …}` on `/v1/graph/why-built`.
- **`what_breaks_if_changed`** — reverse-dependency blast radius: which services/repos depend on the target (and who owns them). Bounded by `max_hops` (ceiling 6) over the `depends_on` adjacency list via the backend's recursive-CTE `GraphRetriever`.
- **`experts_on`** — people ranked by authored/reviewed/expert signal on a topic, with evidence.
- **`graph_neighbors`** — typed incoming + outgoing neighbours of an entity (`max_hops` ceiling 4). The general-purpose graph-walk primitive.
- **`incident_root_cause`** — root cause + remediation for an incident, grounded in cited evidence. Routes through `/v1/copilot/ask` with the templated question `"What was the root cause and remediation of this incident: {incident}? Cite the evidence."`
- **`how_does_x_work`** — how a subsystem/feature works, grounded in the engineering memory with citations. Routes through `/v1/copilot/ask` with `"How does {topic} work in this codebase?"`

### 2.2 The dispatch table

The full request-side routing lives in `_dispatch(tool, args, backend, *, agent_jwt)` in `api/invoke.py`. Each branch reads only the manifest-validated args it needs, calls the backend with the resolved `agent_jwt`, and reshapes the response into `(output, citations)`:

- **Graph tools** return `{"items": data.get("items", [])}` as `output` and `data.get("citations", [])` as `citations`.
- **Copilot tools** return `{"answer": data.get("answer", "")}` as `output` and `data.get("citations", [])` as `citations`.

Integer coercion for the optional hop args is explicit (`int(args.get("max_hops", 3))` / `int(args.get("max_hops", 2))`) so the default applies when the arg is omitted and the manifest's min/max bounds have already been enforced when it is present. An unrecognised tool that somehow reaches `_dispatch` raises `ApiError(NOT_FOUND, …)` — but in practice the tool-name guard in the handler (§4.4) rejects unknown tools first.

---

## 3. The Contract-4 manifest

`manifest.json` (at the package root, located via `MANIFEST_PATH`, default `./manifest.json`) is the **single committed source of truth**. It is loaded once (`@lru_cache load_manifest()`), drives both `GET /manifest` and per-tool input validation, and is validated against `contracts/mcp/manifest.schema.json` in CI.

### 3.1 Top-level manifest fields

| Field | Value | Contract-4 note |
|---|---|---|
| `schema_version` | `"1.0.0"` | version of Contract-4 itself (`pattern ^\d+\.\d+\.\d+$`) |
| `protocol_version` | `"mcp/1.0"` | MCP wire-protocol version (`pattern ^mcp/\d+\.\d+$`) |
| `name` | `"mcp-eng-memory"` | server name, dash-case |
| `display_name` | `"Engineering Memory"` | human-readable |
| `version` | `"1.0.0"` | server implementation version |
| `description` | one-line | required, `minLength 1` |
| `author` | `"CypherX Platform"` | |
| `category` | `"engineering-knowledge"` | |
| `tags` | `["engineering-memory", "knowledge-graph", "code-ownership", "impact-analysis"]` | discovery tags |
| `auth_required` | `true` | a Bearer token is required |
| `required_scopes` | `["tool:invoke", "tool:mcp-eng-memory:invoke"]` | coarse + fine (see §5) |
| `health_endpoint` | `"/livez"` | Contract-7 |
| `metrics_endpoint` | `"/metrics"` | Contract-7 |
| `tools` | array of 7 (`minItems 1`) | each `{name, description, input_schema, …}` |

Tool `name`s are snake_case to match MCP's JSON-RPC method-name convention (`pattern ^[a-z][a-z0-9]*(_[a-z0-9]+)*$`); the server `name` is dash-case (`mcp-eng-memory`). Both conform to the Contract-4 patterns. The manifest's `additionalProperties: true` (mandated by the contract for forward-compat) means a future Phase-7 manifest extension — capability layer, `sandbox_class`, egress allowlist, tenant-tool metadata — is added without a contract version bump and without breaking this validator.

### 3.2 Tool input schemas

Each tool carries an `input_schema` that is a JSON-Schema fragment with `type: "object"`, `additionalProperties: false`, `properties`, and `required`. Three representative shapes:

```jsonc
// who_owns
{ "type": "object", "additionalProperties": false,
  "properties": { "target": { "type": "string", "minLength": 1 } },
  "required": ["target"] }

// what_breaks_if_changed
{ "type": "object", "additionalProperties": false,
  "properties": {
    "target":   { "type": "string",  "minLength": 1 },
    "max_hops": { "type": "integer", "minimum": 1, "maximum": 6, "default": 3 } },
  "required": ["target"] }

// graph_neighbors  (max_hops ceiling 4, default 2)
{ "type": "object", "additionalProperties": false,
  "properties": {
    "target":   { "type": "string",  "minLength": 1 },
    "max_hops": { "type": "integer", "minimum": 1, "maximum": 4, "default": 2 } },
  "required": ["target"] }
```

`additionalProperties: false` on every tool input schema is what makes the facade reject unknown caller fields — a defence-in-depth complement to the reserved-key registry enforced deeper in `cypherx-a1`. Only `who_owns` declares an `output_schema` in the committed manifest; the rest omit it (the contract requires only `name`, `description`, `input_schema` per tool), and the facade does not validate outputs against a schema — it only caps their serialized size.

### 3.3 Contract-4 conformance test

The manifest is validated against `contracts/mcp/manifest.schema.json` (the Contract-4 schema, draft 2020-12) as part of the contracts gate. The committed manifest satisfies the schema's `required` set (`schema_version`, `protocol_version`, `name`, `version`, `description`, `tools`) and every field-level `pattern`/`minLength`/`minItems` constraint. Because the contract sets `additionalProperties: true` at the manifest, tool, and `rate_limit` levels, the manifest may grow additively forever without a breaking change.

---

## 4. `POST /mcp/v1/invoke`

The single invocation endpoint. Pipeline (in order), implemented in `invoke()`:

```
auth (coarse tool:invoke)  →  fine scope tool:mcp-eng-memory:invoke
  →  body-size cap (Content-Length)  →  parse {tool, args}
  →  manifest input-schema validation (422 + JSON Pointer)
  →  _dispatch → forward agent JWT to cypherx-a1 backend
  →  output cap  →  cited result envelope
```

### 4.1 Request body

```json
{ "tool": "who_owns", "args": { "target": "cypherx-ai/auth" } }
```

The handler is tolerant about how args arrive: it reads `payload["args"]`, falling back to `payload["arguments"]`, and finally to "all top-level keys except `tool`/`args`/`arguments`". After resolution, `args` must be a JSON object or it is rejected `VALIDATION_ERROR`. `tool` must be present and a known manifest tool, else `NOT_FOUND`.

### 4.2 Response envelope

On success (HTTP 200, `application/json`):

```json
{
  "tool": "who_owns",
  "output": { "items": [ … ] },
  "citations": [ … ],
  "duration_ms": 142,
  "trace_id": "…"
}
```

| Field | Source |
|---|---|
| `tool` | the validated tool name |
| `output` | `{"items": …}` for graph tools, `{"answer": …}` for copilot tools |
| `citations` | `data.get("citations", [])` from the backend response (verbatim) |
| `duration_ms` | `int((time.monotonic() - started) * 1000)`, wall-clock for the dispatch |
| `trace_id` | `trace.trace_id_var.get()` — the W3C trace id for this request |

The body is serialized with `json.dumps(body)` and returned directly via `Response(...)` (not a model), so no field is dropped or reshaped.

### 4.3 The two size caps

| Cap | Setting | Default | Enforced where | Error on breach |
|---|---|---|---|---|
| Request body | `max_request_body_bytes` | `1_048_576` (1 MiB) | `Content-Length` header, before reading the body | `413 PAYLOAD_TOO_LARGE`, `details.reason = "BODY_BYTES_EXCEEDED"` |
| Output | `max_output_bytes` | `10_485_760` (10 MiB) | `len(serialized.encode("utf-8"))` after dispatch | `413 PAYLOAD_TOO_LARGE`, `details.reason = "OUTPUT_BYTES_EXCEEDED"` |

The request cap is checked from the declared `Content-Length` (`if clen and clen.isdigit() and int(clen) > …`) — cheap, before any body is read. The output cap is checked on the actual serialized UTF-8 byte length, after the backend has answered, so an over-large graph fan-out or copilot answer is rejected rather than streamed to the caller. Both are the Contract-4 platform-standard caps (1 MiB / 10 MiB), and both increment `mcp_eng_memory_invoke_rejected_total` with `reason` `output_too_large` (the output path).

### 4.4 Validation and error mapping

Input validation is performed by `manifest_svc.validate_input(tool, args)`, a dependency-free validator (no `ajv`/`jsonschema` import) that walks the tool's `input_schema`:

- `additionalProperties: false` → reject unexpected keys (`SchemaViolation("/{key}", "unexpected property …")`).
- `required` → reject missing keys (`SchemaViolation("/{field}", "missing required property …")`).
- per-property: `type` (`string`/`integer`/`number`), `minLength`, `minimum`, `maximum`. Booleans are rejected as integers (`bool` is a subclass of `int`).

A violation raises `SchemaViolation(pointer, message)`; the handler converts it to `ApiError(VALIDATION_ERROR, …, details={"pointer": <JSON Pointer>, "reason": <message>})` → **HTTP 422** with the Contract-2 envelope. The `pointer` is a JSON Pointer to the offending field (e.g. `/target`, `/max_hops`).

Full error matrix:

| Condition | Code | HTTP | Detail |
|---|---|---|---|
| Missing/malformed `Authorization` | `UNAUTHORIZED` | 401 | — |
| Invalid token / JWKS fetch failure | `UNAUTHORIZED` | 401 | — |
| Token missing `tenant_id` claim | `UNAUTHORIZED` | 401 | — |
| `X-Forwarded-Agent-JWT` with non-service bearer | `UNAUTHORIZED` | 401 | — |
| `on_behalf_of` ≠ forwarded `agent_id` | `UNAUTHORIZED` | 401 | — |
| Missing coarse scope `tool:invoke` | `FORBIDDEN` | 403 | — |
| Missing fine scope `tool:mcp-eng-memory:invoke` | `FORBIDDEN` | 403 | `mcp_eng_memory_invoke_rejected_total{reason="scope_denied"}` |
| Body exceeds 1 MiB (`Content-Length`) | `PAYLOAD_TOO_LARGE` | 413 | `reason=BODY_BYTES_EXCEEDED` |
| Body not valid JSON / not an object | `VALIDATION_ERROR` | 422 | — |
| Unknown / missing `tool` | `NOT_FOUND` | 404 | — |
| `args` not an object | `VALIDATION_ERROR` | 422 | — |
| Input schema violation | `VALIDATION_ERROR` | 422 | `{pointer, reason}`; `reason="schema_invalid"` metric |
| Result exceeds 10 MiB | `PAYLOAD_TOO_LARGE` | 413 | `reason=OUTPUT_BYTES_EXCEEDED` |
| Backend rejected forwarded token (401/403) | `FORBIDDEN` | 403 | — |
| Backend other ≥400 | `SERVICE_UNAVAILABLE` | 503 | — |
| Backend transport error | `SERVICE_UNAVAILABLE` | 503 | "Engineering-memory backend unavailable." |

All errors render through `install_exception_handlers` into the Contract-2 envelope `{ "error": { code, message, request_id, trace_id, timestamp, [details] } }`.

---

## 5. Auth: dual-mode + dual-scope

Inbound verification lives in `require_principal(request)` (`core/auth.py`) and resolves any caller to one `Principal`:

```python
@dataclass
class Principal:
    tenant_id: str
    agent_id: str | None
    scopes: list[str]
    agent_jwt: str          # the agent token FORWARDED to the cypherx-a1 backend
    raw_claims: dict[str, Any]
```

### 5.1 Dual-mode

The presence of the `X-Forwarded-Agent-JWT` header selects the mode:

| | EXTERNAL mode | INTERNAL mode |
|---|---|---|
| Trigger | no `X-Forwarded-Agent-JWT` | `X-Forwarded-Agent-JWT` present |
| Bearer (`Authorization`) | a bare / api-key-exchanged **agent** JWT | a Contract-12 **service** token (`sub` starts `svc:` or `svc-ext:`) |
| Forwarded header | — | the originating agent's JWT |
| Extra check | — | service `on_behalf_of` MUST equal forwarded `agent_id` |
| `tenant_id` | from the bearer's `tenant_id` claim | forwarded JWT's `tenant_id`, falling back to the service token's |
| `agent_jwt` (forwarded downstream) | the bearer itself | the **forwarded** agent JWT |
| `scopes` | from the bearer | from the **service token** (tool scopes live on the calling service principal) |

Both modes verify their token(s) with `_decode()`: RS256 against the Auth JWKS (`PyJWKClient`, `cache_keys=True, lifespan=300`), enforcing `audience = AUTH_PLATFORM_AUDIENCE`, `issuer = AUTH_ISSUER_URL`, a 60-second clock-skew `leeway`, and required claims `["exp", "iss", "aud", "sub"]`. In INTERNAL mode the forwarded agent JWT is *also* decoded and verified, so a forged forwarded token is rejected here, not just at the backend.

EXTERNAL mode is the path an **external coding agent / IDE** takes (it exchanged its API key for an agent JWT at Auth, then calls the facade directly). INTERNAL mode is the path **xAgent** takes when it invokes the tool as part of its tool loop — it presents its service token and forwards the originating agent's JWT.

### 5.2 Dual-scope

Two scope checks, at two layers:

1. **Coarse `tool:invoke`** — checked in `require_principal` (`settings.coarse_scope`). Any caller without it gets `403 FORBIDDEN` before the handler runs. This is the Contract-4 "may invoke any tool" gate.
2. **Fine `tool:mcp-eng-memory:invoke`** — checked at the top of the `invoke()` handler (`settings.fine_scope`). Missing it → `403 FORBIDDEN` + `mcp_eng_memory_invoke_rejected_total{reason="scope_denied"}`. This is the Contract-4 "may invoke *this server*" gate.

Both scopes are declared in the manifest's `required_scopes`, matching the Contract-4 scope granularity model (coarse `tool:invoke`, fine `tool:<server-name>:invoke`, wildcard `tool:*:invoke` reserved for admin/platform). A `Principal` is admitted to a tool only when it carries **both**.

### 5.3 Reserved JWT claims

The facade is forward-compatible with the Phase-13 reserved claims (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`): `_decode()` does not require them and `require_principal` does not gate on them. Tokens carrying them verify and pass; tokens lacking them are not penalised. This matches the platform invariant: accept-but-ignore the reserved claims until their enforcement phase.

---

## 6. Statelessness invariant (no DB, no Kafka, metering at the caller)

This is the central design rule and is enforced by construction:

- **No database.** The package has no `psycopg`, no pool, no schema. `main.lifespan` builds only a `BackendClient` and warms JWKS — it never opens a DB connection.
- **No Kafka, no outbox.** There is no `aiokafka`, no `outbox` table, no publisher. The facade emits **no** events of any kind.
- **No metering.** Per-invocation tool metering (`cypherx.tools.invocation.metered`) is the **calling xAgent's outbox**, per the platform eventing model — the stateless tool server never meters. The docstring of `invoke.py` states this explicitly: *"This server is stateless: NO metering is emitted here — the calling agent's (xAgent) outbox owns per-invocation metering (Contract 14)."* The `cypherx-a1` product service meters its **own** usage on its own topic (`cypherx.cypherxa1.usage.recorded`), but that happens behind the backend boundary, not in the facade.
- **No revocation store.** The facade is Valkey-free **by design**. Live token revocation (the shared kill-switch mirror) is enforced at the `cypherx-a1` backend the facade forwards to: the backend independently re-verifies the agent JWT — signature, claims, *and* the revocation mirror — before touching the graph. The facade carries the `REVOCATION_*`/`VALKEY_URL` settings for symmetry with the product service but does not consult them on the hot path.

The only mutable state in the process is the in-memory JWKS key cache and the `@lru_cache`d manifest — both immutable from the caller's perspective. The facade is therefore freely horizontally scalable and restart-safe with zero coordination.

### 6.1 The proxy boundary

The `BackendClient` (`services/backend.py`) is the only outbound dependency. It POSTs to `{CYPHERXA1_BASE_URL}{path}` with:

- `Authorization: Bearer <resolved agent_jwt>` — the agent token from the `Principal`, **not** a service token. The backend re-verifies *this* token and enforces tenant RLS from its claims; the facade contributes no tenant logic of its own.
- `trace.propagation_headers()` — the W3C `traceparent` (rebuilt from the request's trace context) plus `X-Request-ID`, so the trace is unbroken across the hop.

Backend status mapping: `401/403` → `FORBIDDEN` (the forwarded token was rejected); any other `≥400` → `SERVICE_UNAVAILABLE`; transport errors (`httpx.HTTPError`) → `SERVICE_UNAVAILABLE` "Engineering-memory backend unavailable." The backend timeout is `BACKEND_TIMEOUT_SECONDS` (default 60s), comfortably above the longest tool ceiling (the 60s copilot tools).

---

## 7. `GET /manifest` — ETag / 304

`get_manifest()` (`api/manifest.py`) serves the committed manifest with a **strong content-addressed ETag** and full `If-None-Match` support:

1. Load the manifest (`manifest_svc.build_manifest()` → the cached `manifest.json`).
2. Compute the ETag: `manifest_etag(manifest)` = `"` + `sha256( json.dumps(manifest, sort_keys=True, separators=(",",":")) )` + `"`. Canonicalised (sorted keys, no whitespace) so the ETag is stable across serialization differences — a true content hash.
3. If `If-None-Match` matches (the helper `_matches` handles comma-separated lists, the `*` wildcard, and `W/`-prefixed weak validators by stripping the prefix), return **304 Not Modified** with just the `ETag` header and `mcp_eng_memory_manifest_served_total{status="304"}`.
4. Otherwise return **200** with the JSON body, `ETag`, and `Cache-Control: no-cache`, and `mcp_eng_memory_manifest_served_total{status="200"}`.

`GET /manifest` is **unauthenticated** — it advertises capability, not data, and is what the tool-registry health-poll and external IDEs read to discover the tool surface. The ETag is the registry's cache key: it polls with `If-None-Match`, treats `200` as "manifest changed" and `304` as "unchanged", so an unchanged manifest costs one cheap conditional request.

---

## 8. `/livez` `/readyz` `/metrics` (Contract 7)

| Endpoint | Checks | Body / status |
|---|---|---|
| `GET /livez` | process-only (Contract-7: liveness NEVER checks downstreams) | `{status:"ok", version, uptime_seconds}`, always 200 |
| `GET /readyz` | warm Auth JWKS (`get_jwks_client(...).get_signing_keys()`) | `{ready, checks:{auth_jwks:"ok"|"fail"}}`; **200** if JWKS warm, **503** if not |
| `GET /metrics` | — | Prometheus exposition (`generate_latest()`, `CONTENT_TYPE_LATEST`) |

`/livez` is purely `{status, version, uptime_seconds}` from a `_START = time.monotonic()` baseline — it answers "is the process alive", nothing more, satisfying the Contract-7 rule that liveness must not probe dependencies. `/readyz` gates on the **single** readiness signal that matters for a stateless facade: can it verify tokens? It checks the Auth JWKS is reachable/warm. It deliberately does **not** probe the `cypherx-a1` backend — a backend blip is surfaced per-request as `503 SERVICE_UNAVAILABLE`, not as a readiness flap that would pull the facade out of rotation. The readiness check never raises (a JWKS error is caught and reported as `auth_jwks: "fail"`).

### 8.1 Metrics

Defined in `core/metrics.py` (Contract 7):

| Metric | Type | Labels |
|---|---|---|
| `mcp_eng_memory_invoke_total` | Counter | `tool`, `outcome` |
| `mcp_eng_memory_invoke_rejected_total` | Counter | `reason` (`scope_denied`, `schema_invalid`, `output_too_large`) |
| `mcp_eng_memory_invoke_duration_seconds` | Histogram | `tool` |
| `mcp_eng_memory_manifest_served_total` | Counter | `status` (`200`, `304`) |
| `mcp_eng_memory_revocation_checks_total` | Counter | `outcome` |

The invoke histogram is observed around `_dispatch` only (`with metrics.invoke_duration_seconds.labels(tool).time()`), so it measures backend-call latency, not auth/validation overhead. `invoke_total{outcome="ok"}` is incremented once a result passes the output cap.

---

## 9. Trace + Contract-2 wiring

`TraceContextMiddleware` (`core/trace.py`) runs first: it parses an inbound W3C `traceparent` (rejecting the all-zero trace/span and malformed shapes via `parse_traceparent`), or mints a fresh trace/span, binds `request_id`/`trace_id`/`span_id` into context vars and structlog (Contract 6 JSON logs), and stamps `X-Request-ID` onto every response. `propagation_headers()` reconstructs the `traceparent` (+ `tracestate`, `X-Request-ID`) for the backend hop, so a coding agent's trace flows agent → facade → `cypherx-a1` → llms-gateway/guardrails/rag unbroken.

Errors render through `install_exception_handlers` (`core/errors.py`) into the Contract-2 envelope. `ApiError` carries `code`, `message`, an HTTP status mapped from `ErrorCode` (`VALIDATION_ERROR→422`, `UNAUTHORIZED→401`, `FORBIDDEN→403`, `NOT_FOUND→404`, `PAYLOAD_TOO_LARGE→413`, `RATE_LIMIT_EXCEEDED→429`, `SERVICE_UNAVAILABLE→503`, `INTERNAL_ERROR→500`), and optional `details`/`headers`. Every envelope includes `request_id`, `trace_id`, and a `timestamp` (RFC-3339 UTC, ms precision, `Z`).

---

## 10. Tool-registry registration

`mcp-eng-memory@1.0.0` is registered with the SharedCore **Tool Registry** so AI coding agents (and xAgent's tool loop) can discover and resolve it by `name@version`:

- **Registration (ops-time):** `POST /v1/tools` on the tool-registry, supplying the server identity and `/manifest` URL. This is an operator action performed by the `cypherx-a1` service principal (a Contract-12 service token + agent forwarding); the boundary is documented in `docs/02-sharedcore-integration-boundary.md` §2.
- **Discovery + resolve:** agents call the registry's `GET /v1/tools` / `GET /v1/tools/{name}` to resolve `mcp-eng-memory@1.0.0` to its base URL, then read `GET /manifest` and invoke `POST /mcp/v1/invoke`.
- **Health poll:** the registry's poller is ETag-aware against `GET /manifest` (`If-None-Match` → 200 = changed, 304 = unchanged), so an unchanged manifest is a cheap conditional. The facade's `health_endpoint` is `/livez` and `metrics_endpoint` is `/metrics`, both declared in the manifest.
- **Metering ownership:** registration does **not** make the registry or the tool meter invocations. Per-invocation tool metering remains the **caller's (xAgent) outbox** — the registry is a catalogue and health poller, not a billing path.

### 10.1 `auth.service_acl` seeding

So the `cypherx-a1` service principal may mint the Contract-12 service tokens it needs (including for tool-registry interactions and the downstream SharedCore calls behind the facade), the `auth.service_acl` edges are seeded by **cypherx-a1's own** `db/migrations/20260614_0002__seed.sql`, using the **canonical** columns `(caller_service, target_service, allowed_scopes)` — never the rag-seed's buggy `(source_service, scopes)`. The compose migrate job applies cypherx-a1 *after* auth, so `auth.service_acl` already exists; the insert is idempotent (`ON CONFLICT (caller_service, target_service) DO NOTHING`). Seeded edges:

| caller_service | target_service | allowed_scopes |
|---|---|---|
| `cypherx-a1` | `auth-service` | `internal:read` |
| `cypherx-a1` | `llms-gateway` | `internal:read`, `internal:write` |
| `cypherx-a1` | `guardrails-service` | `internal:read`, `internal:write` |
| `cypherx-a1` | `rag-service` | `internal:read`, `internal:write` |
| `cypherx-a1` | `memory-service` | `internal:read`, `internal:write` |

The facade itself mints no service token — it *forwards* the caller's agent JWT. The service-token machinery is the product service's; these ACL edges back the calls the backend makes on the far side of the proxy boundary.

---

## 11. The external-IDE path

The facade is the integration seam for **AI coding agents and external IDEs** (Cursor, Claude Code, Copilot-style agents) that want to query the organisation's engineering memory inline. The flow:

1. **Get an agent JWT.** The external agent exchanges its CypherX **API key** for an agent JWT at Auth (the standard api-key → JWT exchange). The JWT carries `tenant_id`, `agent_id`, and the scopes `tool:invoke` + `tool:mcp-eng-memory:invoke`.
2. **Discover the manifest.** `GET /manifest` (unauthenticated) → the seven tools + their input schemas. The IDE renders them as callable tools; the ETag lets it cache the manifest cheaply.
3. **Invoke.** `POST /mcp/v1/invoke` with `Authorization: Bearer <agent JWT>` (EXTERNAL mode — no `X-Forwarded-Agent-JWT`) and body `{tool, args}`.
4. **Cited result.** The facade validates, forwards the JWT to `cypherx-a1` (which re-verifies it incl. revocation, sets `app.tenant_id`, enforces RLS, runs hybrid retrieval / copilot / guardrails), and returns `{tool, output, citations, duration_ms, trace_id}`. Every answer is **source-cited** — the coding agent can show the developer *which* PR / decision / incident / file grounds the claim.

This is the EXTERNAL-mode path (§5.1). When the same tool is invoked **inside** the platform — by xAgent's tool loop — the request arrives in INTERNAL mode (service token + `X-Forwarded-Agent-JWT`), and xAgent's outbox owns the metering. Either way, the facade's behaviour is identical: authenticate, validate, forward, cap, return. It holds no session, no token store, and no state between calls, so an external IDE and an internal agent can hit any replica interchangeably.

---

## 12. Invariants (do NOT break)

- **Stateless forever.** Never add a DB, Kafka, outbox, or session store to `mcp-eng-memory`. The package must stay free of `psycopg`/`aiokafka`. If you need state, it belongs in `cypherx-a1`, behind the proxy boundary.
- **The facade never meters.** Per-invocation metering is the calling xAgent's outbox. The product service meters its own usage on its own topic. The facade emits no events.
- **Identity from the token only.** The body carries `tool`/`args` and nothing else identity-bearing; `tenant_id`/`agent_id` come from the verified JWT. Tool input schemas use `additionalProperties: false`.
- **Both scopes, always.** Coarse `tool:invoke` (in `require_principal`) *and* fine `tool:mcp-eng-memory:invoke` (in the handler). Do not collapse them.
- **Dual-mode `on_behalf_of` binding.** In INTERNAL mode, the service token's `on_behalf_of` MUST equal the forwarded agent's `agent_id`, and the bearer MUST be a `svc:`/`svc-ext:` subject.
- **Caps are platform-standard.** 1 MiB request / 10 MiB output (Contract-4). Don't widen them silently.
- **`/livez` never probes downstreams; `/readyz` gates only on JWKS** — not on the `cypherx-a1` backend (a backend blip is a per-request 503, not a readiness flap).
- **The manifest is the single source of truth.** Add/alter tools by editing `manifest.json` (and the `_dispatch` branch); it must keep validating against `contracts/mcp/manifest.schema.json`. Forward-compat (`additionalProperties: true`) means additive manifest growth needs no contract bump.
- **Valkey-free by design.** Revocation is enforced at the backend the facade forwards to — do not add a revocation store to the facade.
