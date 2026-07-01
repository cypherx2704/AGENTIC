# CypherX xAgent — agent-runtime

Single-agent task-execution service for the CypherX platform. Accepts a task over a
bare agent JWT, runs it through a **named-stage pipeline**
(LOAD → guardrails → LLM → guardrails → return), and returns a Contract-3 response. The
runtime is the edge service that re-verifies inbound agent JWTs, holds each agent's
runtime configuration, enforces guardrails on input and output, and records a per-step
audit trail.

- Python 3.12 · FastAPI · `uv` · psycopg3 (async, RLS) · structlog · Prometheus
- Soft dependencies: Valkey (cache / signals), Kafka (event outbox)
- Hard dependencies at boot: PostgreSQL, Auth JWKS

---

## Running locally

```bash
# 1. Install (base — tests + dev tooling)
uv sync

# 2. Provide the one required secret (matches your local Auth config)
export SERVICE_BOOTSTRAP_SECRET=test-xagent-bootstrap-secret

# 3. Run the API (reload for dev)
uv run uvicorn agent_runtime.main:app --reload --port 8087
```

Everything else defaults to a localhost developer machine (DB, Kafka, Auth, downstream
LLMs/Guardrails URLs), so the service boots without a populated environment. Postgres and
the Auth JWKS are the only hard dependencies — `/readyz` reports `503` until both are
reachable; Kafka, Valkey, and the downstream services are soft (their outages are handled
fail-soft on the task path and never flip the service un-ready).

The four-service local run (Auth + Guardrails + LLMs-gateway + xAgent) follows the shared
host-shell recipe; the default downstream ports are `llms=8085`, `guardrails=8086`,
`auth=8080`.

### Tests / lint

```bash
uv run pytest          # network-free: DB pool nulled, downstream calls respx-mocked
uv run ruff check src tests
uv run mypy
```

---

## Configuration (env vars)

All settings are read from the process environment (no prefix; Doppler-compatible). See
`src/agent_runtime/core/config.py` for the authoritative list + documented defaults.
Highlights:

| Var | Default | Notes |
| --- | --- | --- |
| `SERVICE_BOOTSTRAP_SECRET` | *(required)* | Bootstrap secret to mint xAgent service tokens at Auth. Fails fast if unset. |
| `DATABASE_URL` | `postgresql://xagent_user:localdev@localhost:5432/cypherx_platform` | RLS-scoped runtime role (not superuser). |
| `VALKEY_URL` | `redis://localhost:6379/0` | Soft dependency. Cache + cancel/idempotency signals. |
| `AUTH_JWKS_URL` / `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE` | `http://localhost:8080…` / `cypherx-platform` | Inbound agent-JWT verification (Contract 1). |
| `AUTH_SERVICE_URL` | `http://localhost:8080` | Service-token minting + `GET /v1/agents/{id}` cross-validation. |
| `LLMS_GATEWAY_URL` / `GUARDRAILS_SERVICE_URL` | `http://localhost:8085` / `:8086` | Downstream first-cycle dependencies. |
| `KAFKA_BROKERS` | `localhost:9092` | Transactional outbox drain target. |
| **Agent-config cache** | | Read-through Valkey cache for the LOAD stage. |
| `AGENT_CONFIG_CACHE_ENABLED` | `true` | OFF → LOAD reads straight through Postgres. |
| `AGENT_CONFIG_CACHE_TTL_SECONDS` | `300` | 5-min TTL backstops a missed invalidation. |
| `AGENT_CONFIG_CACHE_KEY_PREFIX` | `cypherx:xagent:agentcfg:` | `agent_id` is appended. |
| **OpenTelemetry** | | Span export is opt-in; W3C propagation is always on. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(empty → disabled)* | Set to a collector URL to export spans. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | `grpc` (:4317) or `http/protobuf` (:4318). |
| `OTEL_SERVICE_NAME` | `agent-runtime` | `service.name` resource attribute. |
| **Pipeline stage flags** | | `STAGE_ENABLE_<NAME>` flip a registry slot. |
| `STAGE_ENABLE_LOAD` / `…PRE_GUARDRAIL` / `…PROMPT_BUILD` / `…LLM` / `…POST_GUARDRAIL` | `true` | First-cycle stages. Enhancement slots default OFF. |

Reliability knobs (cancel / timeout / idempotency / authorize / sweeper) and the live
token-revocation mirror are also env-overridable — see `core/config.py`.

---

## Endpoints

### Tasks (Component 2)
- `POST /v1/tasks` — submit + synchronously execute a task (`mode=sync` only, first
  cycle); returns the Contract-3 response. Body: `{agent_id, input:{message}, mode,
  priority?, timeout_seconds?, metadata?}`. Identity (tenant/agent) comes from the JWT
  only — never the body (Contract 13). `agent_id` must equal the caller's own agent.
- `GET /v1/tasks/{task_id}` — fetch a task (RLS-scoped). **Honest projection**: an
  in-flight task reports its real `pending` / `running` status with the audit steps
  written **so far** (per-stage write-through makes mid-run steps visible).

### Agent runtime config (Component 1)
Admin-only management surface (`agent:admin` or `platform:admin`). Writes cross-validate
the target agent against Auth identity (existence + tenant match).

- `GET /v1/agents/{agent_id}/runtime` — return the runtime config + `status` +
  `runtime_version`. `404` when no runtime row exists for this tenant.
- `PUT /v1/agents/{agent_id}/runtime` — **upsert** the config:
  - **create** on first write (status + version from the body, default `1.0.0`); returns `201`.
  - **update** thereafter: validates the **status transition**, **bumps**
    `runtime_version` (patch increment, e.g. `1.0.0 → 1.0.1`), writes, then **invalidates**
    the agent-config cache key.
  - Status transitions: `pending_config → {active, inactive}`; `active ↔ inactive`;
    self-transition `X → X` always allowed; regressing to `pending_config` is rejected
    (`409 CONFLICT`).
- `POST /v1/agents/{agent_id}/runtime` — **back-compat** create-only (idempotent, ON
  CONFLICT DO NOTHING). A duplicate POST returns the **existing row unchanged** — use PUT
  to modify.

### Capabilities (Component 7)
- `GET /v1/capabilities` — the calling agent's advertised capabilities (the agent's own
  read path; resolved from `principal.agent_id`).

### Health (Contract 7)
- `GET /livez` — process-only liveness.
- `GET /readyz` — gated on PostgreSQL + a warm Auth JWKS (hard). Valkey is soft-reported.
- `GET /metrics` — Prometheus exposition.

---

## The pipeline (Component 3)

The execution engine is a **pipeline of named, independently feature-flagged stages**, so
Tools / Memory / Skills / RAG can be added later as new stages without re-architecting.
First-cycle order:

```
LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → (EVENT, always last)
```

| Stage | Does | Audit step |
| --- | --- | --- |
| **LOAD** | Resolve the agent's runtime config via the agent-config **read-through cache** (fail-open to an RLS-scoped DB read). `409 CONFLICT` if unconfigured or not `active`. Seeds the user message. | — |
| **PRE_GUARDRAIL** | `POST /v1/check/input`. `allow`/`warn` → proceed; `redact` → rewrite prompt; `block` → `GUARDRAIL_VIOLATION` (422). | `guardrail_check_input` |
| **PROMPT_BUILD** | Assemble `[system, user]` messages. | — |
| **LLM** | One `POST /v1/chat/completions` round-trip with `min(max_tokens, token_budget_per_task)`. Accumulates tokens + cost; validates `finish_reason`. | `llm_call` |
| **POST_GUARDRAIL** | `POST /v1/check/output` (passes the original user message for echo-vs-leak PII). | `guardrail_check_output` |
| **EVENT** | Finally-stage (runs on every path): backstop-persists any un-written steps, then finalises the task row **and** emits the terminal Kafka event **atomically** via the transactional outbox. | — |

Enhancement slots (`MEMORY_RETRIEVE`, `RAG_QUERY`, `SKILL_LOAD`, `TOOL_LOOP`,
`MEMORY_WRITE`) are present in the registry but disabled until their phases land.

A successful task writes exactly the three audit rows above (Contract 15 #7). Downstream
calls carry identity in headers only (forwarded agent JWT + xAgent service token) plus
W3C trace context — never in the body.

### Per-stage step write-through
Each user-visible stage **persists its `task_steps` row as it completes** (write-through),
not in a single post-hoc flush, so `GET /v1/tasks/{id}` shows ordered steps even mid-run.
A step-write is **fail-soft**: a failure is logged and the row stays buffered with
`persisted = False`, and the EVENT stage **backstops** it (re-inserts only un-persisted
rows, never double-inserting). A step-write failure never fails the task.

### Agent-config cache + invalidation
The LOAD stage reads each agent's runtime config through a Valkey read-through cache keyed
by `agent_id` (5-min TTL). It **fails open**: a miss, a disabled/absent cache, a
cross-tenant key collision, a corrupt blob, or any Valkey error all fall through to the
RLS-scoped DB read — so a cache outage never fails a task (at worst the read is as slow as
the uncached path). `PUT /v1/agents/{id}/runtime` **busts** the key on every change; the
TTL backstops a missed bust. The cache reads `app.state.valkey` through a narrow
duck-typed interface (`ValkeyClient.client()`); under test the network-free double lacks
it and the cache transparently bypasses to a DB read.

---

## Reliability features

- **Cancel / timeout** — cooperative cancel signals + a per-task in-process timeout guard;
  a backup sweeper finalises tasks stuck past their deadline (see `core/config.py` and
  `services/sweeper.py`). Cancel store outages degrade fail-soft.
- **Idempotency** (Contract 9) — `Idempotency-Key` on `POST /v1/tasks` reserves the key
  (Valkey `SET NX`) and replays the stored terminal response on a duplicate; a still
  in-flight duplicate is `409`. Fail-closed only when a configured Valkey errors; with no
  Valkey configured it allows through.
- **Live token revocation** (Component 3c) — after signature/claims pass, the inbound JWT
  is checked against the shared Valkey kill-switch (revoked `jti` / poisoned `kid` /
  agent revoke-all epoch). **Fail-open**: a Valkey outage accepts the token (+ log +
  metric) so the kill-switch never becomes an availability risk.
- **Transactional outbox** — the task-row UPDATE and the Kafka event INSERT share one
  tenant transaction, so the row and the event can never diverge; a background publisher
  drains the outbox (DLQ after retries).
- **Tenant isolation (RLS)** — the runtime role is not a superuser; every tenant-scoped
  query runs inside a transaction that sets `app.tenant_id`, and RLS policies admit only
  that tenant's rows. Cross-tenant rows surface as `404`, never leaking existence.
- **Distributed tracing** — W3C `traceparent` **and** `tracestate` are parsed at ingress
  and **propagated verbatim** on every downstream call (`tracestate` is sanitised to the
  W3C limits before forwarding). OTel span **export** is opt-in (set
  `OTEL_EXPORTER_OTLP_ENDPOINT` + install the `otel` extra); with the endpoint unset it is
  a complete NO-OP, so local/test runs need no collector. Header propagation works
  regardless of whether spans are exported.
