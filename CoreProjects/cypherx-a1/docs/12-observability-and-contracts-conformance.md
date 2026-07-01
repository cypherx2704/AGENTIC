# Observability & contracts conformance

> A line-by-line map of where cypherx-a1 implements each cross-service platform contract — errors (2), Kafka (5), logs (6), health (7), tracing (8), idempotency (9), service-token (12), tenant-RLS (13), migrations (14), usage (19) — plus the full logging/metrics/tracing wiring, the health endpoints, and the Prometheus metrics catalog. Every path, function, table, and column named below is taken verbatim from the code.

cypherx-a1 is a **consuming app** (peer of `xAgent/ax-1`), not a SharedCore service. It therefore *honours* the platform contracts on its own request path rather than *defining* any. Its observability stack mirrors the xAgent ax-1 template verbatim in behaviour: structlog JSON to stdout (Contract 6), W3C trace-context propagation + opt-in OTLP export (Contract 8), a small Prometheus metric set on `/metrics` (Contract 7), and the Contract 2 error envelope on every failure.

---

## 1. Conformance scorecard

| Contract | What it governs | cypherx-a1 implementation site | Status |
| --- | --- | --- | --- |
| **1** — Identity / JWKS | RS256 agent-JWT verify, JWKS, revocation mirror | `core/auth.py` (`_decode`, `get_jwks_client`, `_enforce_revocation`) | ✅ |
| **2** — Error envelope | `{ "error": { code, message, details? } }` + code list | `core/errors.py` (`ApiError`, `install_exception_handlers`) | ✅ |
| **5** — Kafka envelope | Event envelope, `partition_key=tenant_id`, topic naming, transactional outbox + `.dlq` | `db/outbox.py` (`build_envelope`, `enqueue_event`, `OutboxPublisher`) | ✅ |
| **6** — Structured logs | JSON to stdout with correlation fields | `core/logging.py` (`configure_logging`) | ✅ |
| **7** — Health / metrics | `/livez`, `/readyz`, `/metrics` | `api/health.py`, `core/metrics.py` | ✅ |
| **8** — Tracing | W3C `traceparent`/`tracestate` propagation + OTLP export | `core/trace.py` (`TraceContextMiddleware`, `propagation_headers`, `init_tracing`) | ✅ |
| **9** — Idempotency | `Idempotency-Key` on downstream writes | `services/llms_client.py`, `services/rag_client.py`, `services/memory_client.py` | ✅ |
| **12** — Service token | Mint + forward service JWT; `X-Forwarded-Agent-JWT` | `services/service_token.py`; `_headers()` in every downstream client | ✅ |
| **13** — Tenant RLS | `SET LOCAL app.tenant_id`, FORCE RLS, identity-from-JWT | `db/pool.py` (`in_tenant`), `db/migrations/…_0001__init.sql` | ✅ |
| **14** — Migrations | Atlas; runtime role vs. DDL role split | `db/migrations/` (`atlas.hcl`, `*__init.sql`, `*__seed.sql`) | ✅ |
| **19** — Usage metering | App emits its OWN usage; never rewrites gateway cost | `extraction_jobs` ledger; `usage_topic`; outbox `record_event` | ✅ |
| **4** — MCP manifest | (`mcp-eng-memory` only) | `mcp-eng-memory/manifest.json` | ✅ (sibling pkg) |

The five contracts cypherx-a1 *does not* author (3 A2A, 10 quota, 11 webhooks-out, …) are not in scope: it is not an agent runtime and not a SharedCore product.

---

## 2. Contract 2 — error envelope

**Site:** `src/cypherx_a1/core/errors.py`.

Every error — raised `ApiError`, FastAPI `RequestValidationError`, Starlette `HTTPException`, or an unhandled `Exception` — is rendered by `_render()` into the canonical envelope:

```json
{
  "error": {
    "code": "GUARDRAIL_VIOLATION",
    "message": "Question blocked by input guardrails.",
    "request_id": "<X-Request-ID>",
    "trace_id": "<correlation trace id>",
    "timestamp": "2026-06-14T00:00:00.000Z"
  }
}
```

`request_id` and `trace_id` are pulled from the trace contextvars (`trace.request_id_var`, `trace.trace_id_var`), so every error is correlatable to its log lines and downstream calls. `details` is added only when present (e.g. validation `errors`).

### Error-code catalog (`ErrorCode`)

| Code | Default HTTP | Raised when |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | Request-body validation fails (`extra="forbid"` models); also the fallback for a 4xx Starlette `HTTPException`. |
| `UNAUTHORIZED` | 401 | Missing/malformed `Authorization`, bad signature, `iss`/`aud`/`exp`/`sub` failure, missing `tenant_id` claim. |
| `TOKEN_REVOKED` | 401 | Revocation mirror reports the token revoked (`_enforce_revocation`). |
| `FORBIDDEN` | 403 | Principal holds none of the required scopes (`require_scope`, `_resolve_principal`). |
| `NOT_FOUND` | 404 | Mapped from a 404 `HTTPException`. |
| `CONFLICT` | 409 | Mapped from a 409 `HTTPException`. |
| `GUARDRAIL_VIOLATION` | 422 | Copilot input/output blocked by guardrails (`decision="block"`). **Product domain code.** |
| `BUDGET_EXCEEDED` | 402 | Reserved (cost gate). |
| `RATE_LIMIT_EXCEEDED` | 429 | Mapped from a 429 `HTTPException`. |
| `SERVICE_UNAVAILABLE` | 503 | A downstream SharedCore call fails or is rejected (llms/guardrails/rag/memory/auth token-mint). |
| `INTERNAL_ERROR` | 500 | Any unhandled exception (`_handle_unexpected`). |

`GUARDRAIL_VIOLATION` and the `RESERVED_METADATA_KEY` reason (surfaced as `VALIDATION_ERROR` with `details.reason="RESERVED_METADATA_KEY"`) are the two product-specific additions on top of the canonical Contract-2 list; both are forward-compatible (additive codes, never a renamed canonical one).

Logging policy in the handlers: `ApiError` with `status >= 500` logs at `error`, otherwise at `info`; the catch-all `_handle_unexpected` logs `unhandled_exception` at `error` with `exc_info`.

---

## 3. Contract 6 — structured logs

**Site:** `src/cypherx_a1/core/logging.py` (`configure_logging`).

structlog is configured to emit **JSON to stdout** (`JSONRenderer` + `PrintLoggerFactory(file=sys.stdout)`), with the stdlib root logger routed through the same `ProcessorFormatter` so third-party log records share the envelope.

### Envelope fields

| Field | Source |
| --- | --- |
| `timestamp` | `TimeStamper(fmt="iso", utc=True, key="timestamp")` |
| `level` | `add_log_level` |
| `message` | `_rename_event_to_message` (renames structlog's `event` → `message`) |
| `service` | `_add_service_fields` → `settings.service_name` (`cypherx-a1`) |
| `version` | `_add_service_fields` → `settings.service_version` |
| `environment` | `_add_service_fields` → `settings.environment` |
| `trace_id`, `span_id`, `request_id`, `tenant_id`, `agent_id` | merged from structlog **contextvars**, bound per request by `TraceContextMiddleware` |

The correlation fields are not threaded by hand — `structlog.contextvars.merge_contextvars` is the first processor, and `TraceContextMiddleware.__call__` calls `bind_contextvars(request_id, trace_id, span_id, tenant_id, agent_id)` at request entry and `clear_contextvars()` on exit (in a `finally`). Default level is `INFO` (`make_filtering_bound_logger(logging.INFO)`).

This is byte-for-byte the xAgent ax-1 logging config, so a single Loki/Promtail pipeline parses both services identically.

---

## 4. Contract 8 — tracing & W3C propagation

**Site:** `src/cypherx_a1/core/trace.py`.

### 4.1 Inbound parse (`TraceContextMiddleware`)

A pure-ASGI middleware runs first on every HTTP request and:

1. Reads `x-request-id` (generates a UUID fallback + logs `request_id_generated_fallback` if absent).
2. Parses `traceparent` via `parse_traceparent()` → `(trace_id_uuid, span_id_hex)`. Malformed/all-zero trace or span IDs are rejected and a fresh `trace_id` (UUID) + `span_id` (16-hex) are minted.
3. Sanitizes `tracestate` via `sanitize_tracestate()` — drops members failing `^[^,=\s]+=[^,=]+$`, caps at 32 members / 256 chars each.
4. Reads `x-tenant-id` and `x-agent-id` headers (correlation only — **never** the authorization source of truth; that is the JWT).
5. Stores all of the above in contextvars (`request_id_var`, `trace_id_var`, `span_id_var`, `tracestate_var`, `tenant_id_var`, `agent_id_var`) and binds them into structlog.
6. Echoes `x-request-id` back on the response via `send_wrapper`.

`parse_traceparent` validates the 4-part `version-trace-span-flags` shape, 32-hex trace + 16-hex span, rejecting the all-zero sentinels — exactly the W3C rules.

### 4.2 Outbound propagation (`propagation_headers`)

Every SharedCore client builds its downstream header set with `**trace.propagation_headers()`, which emits:

| Header | Value |
| --- | --- |
| `traceparent` | `current_traceparent()` → `00-<trace_hex>-<span_hex>-01` rebuilt from the bound trace/span (fresh if unbound) |
| `X-Request-ID` | `request_id_var.get()` |
| `tracestate` | included only if a sanitized non-empty `tracestate` is present |

This guarantees the **same distributed trace** flows through auth → llms → guardrails → rag → memory, and into every Kafka event (the outbox envelope copies `trace_id` — see §5).

### 4.3 OTLP span export (opt-in, NO-OP by default)

`init_tracing(settings)` wires an OTLP exporter **only if** `OTEL_EXPORTER_OTLP_ENDPOINT` is set **and** the OpenTelemetry SDK is installed:

- No endpoint → sets gauge `cypherxa1_otel_tracing_enabled = 0`, logs `otel_tracing_disabled`, returns.
- Endpoint set → `_build_tracer_provider()` chooses gRPC (`otel_exporter_otlp_protocol="grpc"`, default) or HTTP (`http`/`http/protobuf`) exporter, wraps a `BatchSpanProcessor`, tags `Resource{service.name=otel_service_name}` (default `cypherx-a1`), sets gauge `= 1`, logs `otel_tracing_enabled`.
- Any init failure is swallowed (`metrics.otel_tracing_enabled.set(0)`, logs `otel_tracing_init_failed`) — **a tracing-export failure must never fail boot**.

`shutdown_tracing()` flushes the provider on lifespan exit, also failure-tolerant. **Header propagation is independent of the SDK** — W3C trace context flows even when span export is disabled, which is the default local/keyless posture.

---

## 5. Contract 5 — Kafka envelope & transactional outbox

**Site:** `src/cypherx_a1/db/outbox.py` + table `cypherx_a1.outbox`.

### 5.1 Envelope (`build_envelope`)

```json
{
  "event_id": "<uuid>",
  "event_type": "cypherx.cypherxa1.record.normalized",
  "schema_version": "1.0.0",
  "produced_at": "<iso8601 Z>",
  "trace_id": "<correlation trace id>",
  "tenant_id": "<uuid>",
  "producer_service": "cypherx-a1",
  "producer_version": "<service version>",
  "partition_key": "<tenant_id>",
  "payload": { ... }
}
```

`PRODUCER_SERVICE = "cypherx-a1"` and `partition_key = tenant_id` are fixed in code — per-tenant ordering and tenant-keyed partitioning are guaranteed.

### 5.2 Transactional write (`enqueue_event`)

`enqueue_event(conn, …)` inserts into `cypherx_a1.outbox (topic, partition_key, payload)` on the **caller's connection**, inside the same `in_tenant()` transaction as the domain mutation. Row and event therefore can never diverge — in `ingestion/pipeline.py` the vector-ref link, the citation insert, and the `record.normalized` event are written in one `in_tenant(pool, tenant_id, _link)` transaction.

`record_event(pool, …)` is the standalone variant: it opens its own `in_tenant` tx for additive signals not tied to a domain mutation (e.g. usage — see §10).

### 5.3 Publisher (`OutboxPublisher`)

A background task (`cypherxa1-outbox-publisher`, started in the app lifespan) polls every `poll_interval` (2.0s) and drains:

```sql
SELECT id, topic, partition_key, payload, attempts
  FROM cypherx_a1.outbox
 WHERE published_at IS NULL
 ORDER BY created_at
 LIMIT 100
```

Each row is sent with aiokafka `send_and_wait(topic, value=payload, key=partition_key)` (at-least-once); success stamps `published_at = NOW()`. Failures increment `attempts` + record `last_error` (truncated to 2000 chars); at `_MAX_ATTEMPTS = 10` the row is published to `topic + ".dlq"` and marked published. **Kafka unavailable never crashes the request path** — `_ensure_producer()` returns `None` and the events stay durable in the outbox until the next tick.

### 5.4 Topics

| Topic | Emitted by | Payload keys |
| --- | --- | --- |
| `cypherx.cypherxa1.record.normalized` | `ingestion/pipeline.py` (per normalized doc) | `source`, `external_id`, `kb_id`, `doc_id`, `entity_id` |
| `cypherx.cypherxa1.usage.recorded` | `usage_topic` (Contract 19 signal) | usage units + `request_id` (never the gateway cost) |
| `<topic>.dlq` | publisher, after 10 failed attempts | original envelope |

Topics follow the `cypherx.<domain>.<entity>.<event>` convention with domain `cypherxa1`. The MVP **consumes no topics** (the Kafka worker in `worker/runner.py` is a documented scale-out seam).

### 5.5 Outbox table — note the deliberate NO-RLS

`cypherx_a1.outbox` has columns `id, topic, partition_key, payload, created_at, published_at, attempts, last_error` with a partial index `idx_cxa1_outbox_unpublished ON (created_at) WHERE published_at IS NULL`. RLS is **explicitly DISABLED** (`ALTER TABLE cypherx_a1.outbox DISABLE ROW LEVEL SECURITY`) because the publisher drains across all tenants with no `app.tenant_id` set — isolation lives in the payload, not the row. Do not "fix" this.

---

## 6. Contract 7 — health & metrics endpoints

**Site:** `src/cypherx_a1/api/health.py`.

| Endpoint | Method | Gates on | Returns |
| --- | --- | --- | --- |
| `/livez` | GET | nothing (process-only) | `200 {"status":"ok","version","uptime_seconds"}` |
| `/readyz` | GET | **PostgreSQL** (`readyz_ping` → `SELECT 1`, 2s timeout) **AND warm Auth JWKS** (`get_jwks_client(...).get_signing_keys()`) | `{"ready": <bool>, "checks": {...}}`, status `200` if ready else `503` |
| `/metrics` | GET | nothing | Prometheus exposition (`generate_latest()`, `CONTENT_TYPE_LATEST`) |

`/readyz` reports each dependency in `checks`: `postgresql` and `auth_jwks` are **hard** (both must be `ok` for `ready=true`); `valkey` is reported (`ok`/`fail`) only if a client is present and is **soft** — it never gates readiness. Kafka and the downstream SharedCore services are likewise soft (handled fail-soft on the request path) and never gate `/readyz`. `/livez` never touches DB/Kafka/downstream, so a process that is up but with a cold Neon returns `livez=200 / readyz=503` — the intended cold-start posture. This mirrors the xAgent ax-1 health router.

The in-container metrics/app port is the platform-standard `8080` (host `8093`); `/metrics` is scraped by Prometheus regardless of whether OTLP span export is enabled.

---

## 7. Metrics catalog (Prometheus)

**Site:** `src/cypherx_a1/core/metrics.py`. All instruments are `prometheus_client` Counters/Gauges/Histograms (histograms, not summaries, per platform convention).

| Metric | Type | Labels | Incremented / set at |
| --- | --- | --- | --- |
| `cypherxa1_downstream_calls_total` | Counter | `service`, `outcome` | every SharedCore client call. `service ∈ {llms, guardrails, rag, memory}`; `outcome ∈ {ok, error, rejected, forbidden}` (`forbidden` only for a RAG 403 KB-ACL deny). |
| `cypherxa1_copilot_requests_total` | Counter | `outcome` | `copilot/service.py`. `outcome ∈ {ok, blocked_input, blocked_output}`. |
| `cypherxa1_copilot_latency_seconds` | Histogram | — | end-to-end `/v1/copilot/ask` latency (`copilot_latency_seconds.observe(...)`). |
| `cypherxa1_ingestion_records_total` | Counter | `source`, `outcome` | canonical records ingested by source (e.g. `github`) and outcome. |
| `cypherxa1_extraction_jobs_total` | Counter | `outcome` | `extraction/extractor.py`. `outcome ∈ {completed, skipped, failed}`. |
| `cypherxa1_graph_edges_upserted_total` | Counter | `rel` | graph edges upserted, labelled by relation (`owns`, `depends_on`, …). |
| `cypherxa1_revocation_checks_total` | Counter | `outcome` | `core/auth._enforce_revocation`. `outcome ∈ {clean, revoked, skipped, disabled}`. |
| `cypherxa1_revocation_check_skipped_total` | Counter | — | revocation check skipped (fail-open) due to missing/unreachable Valkey. |
| `cypherxa1_otel_tracing_enabled` | Gauge | — | `core/trace.init_tracing` — `1` when OTLP export is wired, else `0`. |

All metric names are prefixed `cypherxa1_` to namespace them in a shared Prometheus. The two revocation counters together let you alert on "verifier running fail-open" (`revocation_check_skipped_total` rising) vs. "active revocations" (`revocation_checks_total{outcome="revoked"}`).

---

## 8. Contract 1 — identity & JWKS verification

**Site:** `src/cypherx_a1/core/auth.py`.

cypherx-a1 is edge-facing: a caller (frontend BFF / edge, or an api-key-exchanged JWT) presents a **bare agent JWT** in `Authorization: Bearer …`. `require_principal` (the FastAPI dependency) re-verifies it locally — defense-in-depth, same posture as xAgent/llms/guardrails/rag:

- **Signature:** RS256 via `PyJWKClient(jwks_url, cache_keys=True, lifespan=300)` (`get_jwks_client`, process-cached, refresh-on-kid-miss); `warm_jwks()` pre-fetches at startup.
- **Claims:** `iss == auth_issuer_url`, `aud` contains `auth_platform_audience`, `exp` valid (±60s `_CLOCK_SKEW_SECONDS`), and `require=["exp","iss","aud","sub"]`.
- **Tenant/agent from JWT only:** `tenant_id` and `agent_id` are read from the verified claims (`_resolve_principal`), **never** a request body — request models are `extra="forbid"`.
- **Scopes:** the principal must hold at least one of `_BASE_ALLOWED_SCOPES` = `{cypherxa1:query, cypherxa1:ingest, cypherxa1:admin, agent:execute, agent:admin, platform:admin}` (else 403). Per-route gating uses `query_scopes()` / `ingest_scopes()` / `admin_scopes()`.
- **Revocation mirror:** after signature/claims pass, `_enforce_revocation` consults the shared Valkey kill-switch (prefix `cypherx:rev:`, 0.15s timeout) by `jti`/`kid`/`agent_id`/`iat`. It **FAILS OPEN** (availability wins) — Valkey down/slow ⇒ `skipped` + skip-counter, never a 5xx.
- **Reserved JWT claims** (`cnf`, `wkl_id`, `behavior_policy_id`, `delegation_*`, `approval_context`) are accepted-but-ignored for Phase-13 hardening — logic never gates on their absence.

The raw verified bearer is preserved on `Principal.raw_token` for forwarding (see §9).

---

## 9. Contract 12 — service token + agent-JWT forwarding

**Site:** `src/cypherx_a1/services/service_token.py` + the `_headers()` method of every downstream client.

cypherx-a1 authenticates to SharedCore as the `cypherx-a1` service principal. `ServiceTokenProvider.get_token(on_behalf_of=…)` mints a short-lived (~5-min) **service JWT** from Auth:

```
POST {auth_service_url}/v1/service-tokens
X-Service-Name: cypherx-a1
X-Service-Bootstrap-Secret: <SERVICE_BOOTSTRAP_SECRET>
{ "on_behalf_of": "<agent_id>" }
```

The token is cached in-process keyed by `on_behalf_of`, refreshed 30s before expiry (`_REFRESH_SKEW_SECONDS`), default TTL 300s. A mint failure raises `SERVICE_UNAVAILABLE` (never a silent 401 downstream — `service_bootstrap_secret` is **required with no default**, so a missing value fails fast at boot).

Every downstream call (llms/rag/guardrails/memory) sends the **dual-identity** header set built by `_headers()`:

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <service-jwt>` (the service principal) |
| `X-Forwarded-Agent-JWT` | the verbatim inbound agent JWT (`Principal.raw_token`) |
| `traceparent` / `X-Request-ID` / `tracestate` | from `trace.propagation_headers()` |
| `Idempotency-Key` | on writes (see §10) |

**Bodies carry no identity.** The downstream verifies that `on_behalf_of` matches the forwarded agent JWT's `agent_id`. The `auth.service_acl` edges authorizing these mints are seeded by cypherx-a1's own `db/migrations/…_0002__seed.sql` using the canonical columns `(caller_service, target_service, allowed_scopes)` — `cypherx-a1 → {auth-service, llms-gateway, guardrails-service, rag-service, memory-service}` with `internal:read`/`internal:write`.

---

## 10. Contract 9 (idempotency) & Contract 19 (usage)

### 10.1 Idempotency-Key on downstream writes (Contract 9)

`Idempotency-Key` is attached on the write paths so a retried worker replays instead of re-spending:

| Call | Idempotency-Key value | Site |
| --- | --- | --- |
| llms-gateway extraction chat | `sha256(tenant_id:node_id:content_sha:extractor_version)` (`_idem_key`) | `extraction/extractor.py` → `llms_client.chat(idempotency_key=…)` |
| RAG inline ingest | `{tenant_id}:{content_sha}:{kb}` | `ingestion/pipeline.py` → `rag_client.ingest_inline(idempotency_key=…)` |
| memory store (episodic) | `{task_id}:mem` | `copilot/service.py` → `memory_client.store(idempotency_key=…)` |

The copilot *answer* chat is intentionally not idempotency-keyed (a fresh question is a fresh generation); only the deterministic, replayable writes are.

### 10.2 Usage metering (Contract 19)

The cardinal rule: **cypherx-a1 meters its OWN usage on its OWN topic and never rewrites the gateway's cost.** The gateway owns `usage.cost_usd` + `llm_call_id` (the billing key); cypherx-a1 only **records** them:

- **Extraction cost ledger** — `extraction_jobs` persists `llm_call_id` and `cost_usd NUMERIC(12,8)` per `(tenant_id, node_id, content_sha, extractor_version)` (`record_extraction_job`). The PK doubles as the idempotency/no-re-spend key — re-ingesting unchanged content never re-bills.
- **Usage event** — the app's own usage signal goes to `usage_topic = "cypherx.cypherxa1.usage.recorded"` via the outbox `record_event` helper, carrying usage **units + request_id**, never an authoritative cost (that lives with the gateway's `llm_call_id`).

The `LlmsClient._parse_chat` deliberately *reads* `usage.cost_usd` and `llm_call_id` (falling back to `id`) from the gateway response — it never computes or overwrites them.

---

## 11. Contract 13 — tenant isolation (RLS)

**Sites:** `src/cypherx_a1/db/pool.py` (`in_tenant`) + `db/migrations/…_0001__init.sql`.

- **Runtime role `cxa1_user`** is `LOGIN`, **not** a superuser, does **not** `BYPASSRLS`, and **cannot `CREATE EXTENSION`** (extensions are created by the DDL role on the frozen `pgvector/pgvector:pg16` image).
- **Per-transaction scoping:** `in_tenant(pool, tenant_id, fn)` opens a transaction and runs `SELECT set_config('app.tenant_id', %s, true)` (transaction-local, equivalent to `SET LOCAL`) before any tenant query. Every repo read/write goes through this helper.
- **FORCE RLS** is enabled on all eleven tenant-scoped tables — `entities, edges, identities, raw_events, connectors, connector_secrets, sync_cursors, extraction_jobs, citations, resource_acls, rag_kbs` — each with a `<table>_isolation` policy:
  ```sql
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  ```
  The `NULLIF(..., '')` guard means an **unset GUC yields no rows** (never an error) — a query that forgets `in_tenant` returns empty, it does not leak.
- **`tenant_id` comes only from the verified JWT** (Contract 1, §8), never a body — closing the spoofing hole at the door.
- **`outbox` is the one exception** (NO RLS) — see §5.5.

Combined: cross-tenant access is architecturally impossible at the row level, regardless of application bugs.

---

## 12. Contract 14 — migrations

**Site:** `db/migrations/` (Atlas — `atlas.hcl`, `schema.sql`, `20260614_0001__init.sql`, `20260614_0002__seed.sql`).

- **`*__init.sql`** is idempotent (re-runnable): `CREATE EXTENSION IF NOT EXISTS pgcrypto`, `CREATE SCHEMA IF NOT EXISTS cypherx_a1`, the runtime role (`CREATE ROLE cxa1_user LOGIN` guarded by `pg_roles`), all tables, indexes, RLS policies, and `GRANT`s. It is applied by the compose `migrate` job against the **Neon DIRECT** DSN (session mode — advisory locks) under the DDL role, **before** any app service starts. The runtime role gets only the column-level privileges it needs (e.g. `raw_events` is `SELECT, INSERT` only; `outbox` is `SELECT, INSERT, UPDATE`).
- **`*__seed.sql`** seeds the `auth.service_acl` edges (§9), guarded on the table existing and on the canonical column names — it applies *after* the auth migration because the compose migrate job orders cypherx-a1 after auth.
- **Role split:** runtime `cxa1_user` (no DDL, no BYPASSRLS, no CREATE EXTENSION) vs. the migration `cxa1_ddl` role — mirrors the platform's runtime/DDL separation. Cloud bootstrap needs Doppler `db/cypherx-a1/{runtime,ddl}_password`; `cypherx_a1` is enrolled in the closed enumerations `infra/dev/local/seed/postgres-init.sql` and `infra/modules/postgres-bootstrap/main.tf`.

---

## 13. End-to-end correlation: one copilot request

To see all the contracts engage at once, trace a `POST /v1/copilot/ask` (`copilot/service.py`):

| Step | Contract(s) engaged |
| --- | --- |
| Request enters → `TraceContextMiddleware` binds `request_id`/`trace_id`/`tenant_id` into structlog + contextvars | 6, 8 |
| `require_principal` verifies the agent JWT (JWKS RS256) + revocation mirror; resolves `Principal` | 1 |
| `require_scope(principal, query_scopes(), "copilot:ask")` | 1 |
| Memory recall → `memory_client.search` (service-token + `X-Forwarded-Agent-JWT` + trace) | 12, 8 |
| PRE-guardrail → `POST /v1/check/input`; `decision="block"` → `422 GUARDRAIL_VIOLATION` | 2, 12 |
| Hybrid retrieve → graph (RLS via `in_tenant`) + RAG-dense (`POST /v1/kbs/{id}/query`) + tsvector | 13, 12 |
| LLM answer → `POST /v1/chat/completions` (gateway owns cost/`llm_call_id`) | 12, 19 |
| POST-guardrail → `POST /v1/check/output` (passes `input_text=question`) | 2, 12 |
| Store episodic memory → `POST /v1/memories` with `Idempotency-Key={task_id}:mem` | 9, 12 |
| Metrics: `cypherxa1_copilot_requests_total{outcome}`, `cypherxa1_copilot_latency_seconds`, `cypherxa1_downstream_calls_total` | 7 |
| Response carries `citations`, `trace_id`, `duration_ms`; `X-Request-ID` echoed | 2, 8 |

Every hop reuses the **same** `traceparent`, the **same** `request_id`, and logs to the **same** JSON envelope — so a single trace_id stitches the browser request, the cypherx-a1 stage log lines, and the auth/llms/guardrails/rag/memory spans into one timeline.

---

## 14. Guards (do NOT "fix")

- **`cypherx_a1.outbox` has NO RLS** — deliberate; the cross-tenant drain needs it (§5.5).
- **Revocation mirror FAILS OPEN** — Valkey is a soft dependency; availability wins (§8). It is reported on `/readyz` but never gates it.
- **OTLP export is opt-in and never fails boot** — absence of `OTEL_EXPORTER_OTLP_ENDPOINT` is the normal/keyless state; header propagation is independent of the SDK (§4.3).
- **Never rewrite the gateway's `cost_usd`/`llm_call_id`** — cypherx-a1 records them, the gateway owns them (§10.2).
- **`auth.service_acl` seed uses canonical columns** `(caller_service, target_service, allowed_scopes)` — not the rag-seed's buggy `(source_service, scopes)` (§9, §12).
- **Identity from JWT only** — `tenant_id`/`agent_id` never from a body; request models are `extra="forbid"` (§8, §11).
