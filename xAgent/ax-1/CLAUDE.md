# CLAUDE.md — xagent (xAgent/ax-1)

> The CypherX **agent-runtime**: the edge service that executes a single agent's task — re-verifies the inbound agent JWT, runs a named-stage pipeline (LOAD → PRE_GUARDRAIL → PROMPT_BUILD → LLM → POST_GUARDRAIL → EVENT), and returns a Contract-3 A2A response. Platform root guide: [../../CLAUDE.md](../../CLAUDE.md).

## What this is
Phase 9 "Single-Agent Runtime" of the CypherX platform (owning spec: `archive/Manoj/phases/phase-09-xagent.md`). A FastAPI/Python service (`POST /v1/tasks`) that accepts a bare agent JWT, runs the task through a **pipeline of named, independently feature-flagged stages**, enforces guardrails on input and output, calls the LLMs gateway, records a per-step audit trail, and emits a terminal Kafka event via a transactional outbox.
**Status: implemented.** First-cycle (9A) + WP08 reliability (cancel/timeout/idempotency/authorize/sweeper/live-revocation, async mode + SSE) are fully built and tested (27 test files). The WP12 enhancement stages (RAG / Memory / Tool-loop / Skills) are **coded and bound but DISABLED by default** (`STAGE_ENABLE_*` flags) pending Phases 5–8. Branch: `development`.

## Tech stack
- **Python 3.12**, **FastAPI**, **uv** (hatchling build, `uv.lock` committed), uvicorn. Module: `agent_runtime` (entrypoint `python -m agent_runtime`).
- **pydantic v2** + **pydantic-settings** (env config, no prefix, Doppler-compatible).
- **psycopg3** async (`psycopg[binary,pool]`) — RLS, runtime role `xagent_user`. **aiokafka** (outbox drain). **redis** client → Valkey. **pyjwt[crypto]** (RS256 JWKS verify). **structlog**, **prometheus-client**.
- Optional `otel` extra (OpenTelemetry OTLP span export) — opt-in; W3C trace-context propagation is always on regardless.
- Tests/lint: pytest, pytest-asyncio (`asyncio_mode=auto`), respx (HTTP mocks), asgi-lifespan; ruff (line-length 110), mypy (`disallow_untyped_defs`).

## Repository layout
| Path | Holds |
| --- | --- |
| `src/agent_runtime/main.py` | App factory + lifespan: opens DB pool, builds token provider + downstream clients, wires Valkey/outbox/sweeper, warms JWKS. Forces `WindowsSelectorEventLoopPolicy` on win32. |
| `src/agent_runtime/api/` | Routers: `tasks.py` (submit sync/async, get, list+cursor, DELETE cancel, SSE stream), `agents.py` (runtime-config GET/PUT/POST), `capabilities.py`, `health.py`. |
| `src/agent_runtime/core/pipeline.py` | The **engine**: `Stage` ABC, `PipelineContext`, `Pipeline` runner, ordered `STAGE_REGISTRY`, between-stage + in-flight-LLM cancel, finally-style EVENT. |
| `src/agent_runtime/core/stages/` | Concrete stages: `load`, `pre_guardrail`, `prompt_build`, `llm`, `post_guardrail`, `event` (+ WP12 `rag_query`, `memory_retrieve`, `memory_write`, `tool_loop`, `skill_load`); `deps.py` holds the shared clients/Valkey holder bound from the lifespan. `tool_loop` ranks+caps offered tools and passes `tool_mode` so SMALL/8B models use tools (the gateway emulates tool-calling); `skill_load` resolves `allowed_skills` + access via `services/skill_registry_client.py` (Skill Registry mirror) and stashes them on `ctx.skills` for PROMPT_BUILD. |
| `src/agent_runtime/core/` | `auth.py` (JWT verify + revocation mirror), `config.py` (all settings, authoritative), `errors.py` (Contract-2 envelope + code→status), `trace.py`, `logging.py`, `metrics.py`. |
| `src/agent_runtime/db/` | `pool.py` (`in_tenant` RLS tx + `readyz_ping`), `tasks_repo`, `steps_repo` (write-through + `StepBuffer`), `agents_repo`, `outbox.py` (transactional outbox + publisher + sweeper finalize). |
| `src/agent_runtime/models/` | `task.py` (public request validation + step-type vocabulary), `a2a.py` (Contract-3 response builder), `agent.py` (runtime config + status transitions). |
| `src/agent_runtime/services/` | Clients: `llms_client`, `guardrails_client`, `rag_client`, `memory_client`, `registry_client`, `mcp_client`, `auth_client`; plus `service_token`, `valkey`, `agent_config_cache`, `sweeper`. |
| `db/migrations/` | `0001` init → `0005` WP12; `0002` seed is a no-op; `schema.sql` (flattened end-state) + `atlas.hcl`. Schema `xagent`. |
| `tests/` | 27 pytest files (network-free: DB pool nulled, publisher/sweeper off, downstreams respx-mocked). |
| `Dockerfile` | Multi-stage uv build; non-root uid 10001; `EXPOSE 8080`; `python -m agent_runtime`; HEALTHCHECK → `/livez`. |

## Build, test, run
**Host (dev):**
```bash
uv sync
export SERVICE_BOOTSTRAP_SECRET=local-dev-xagent-secret   # the ONLY required, no-default secret
uv run uvicorn agent_runtime.main:app --reload --port 8087
uv run pytest            # network-free; healthy run ~10s
uv run ruff check src tests
uv run mypy
```
Everything else defaults to localhost; the service boots without a populated env (Postgres + Auth JWKS are the only hard deps). Local host-shell recipe ports: llms=8085, guardrails=8086, rag=8087, memory=8088, tool-registry=8089.

**Docker / infra/compose** (service `xagent`, build context `xAgent/ax-1`, image `cypherx/xagent:local`):
- In-container port **8080** (`PORT=8080`); host map **8083→8080**.
- `depends_on` (all `service_healthy`): redpanda, valkey, auth-service, llms-gateway, guardrails-service, rag, memory, tool-registry. In-cluster URLs are service-DNS on :8080 (e.g. `http://llms-gateway:8080`).
- Postgres is **external (Neon)** — no postgres container; `DATABASE_URL` from env. Apply migrations via the compose `--profile migrate` job (Atlas).

**Health (Contract 7):** `GET /livez` (process-only), `GET /readyz` (200/503, gated on Postgres + warm Auth JWKS; Valkey soft-reported), `GET /metrics` (Prometheus).

## Configuration & secrets
All env vars (no prefix; Doppler-injected; `core/config.py` is authoritative). Key vars:
- `SERVICE_BOOTSTRAP_SECRET` — **required, no default** (boot fails fast). Mints the xAgent service token at Auth `POST /v1/service-tokens` (`service_principal_name=xagent`). Compose source: `SERVICE_BOOTSTRAP_SECRET_XAGENT`.
- `DATABASE_URL` — RLS runtime DSN (role `xagent_user`, NOT superuser). Compose source `XAGENT_DATABASE_URL`. Neon: pooled for app, direct for migrations, `sslmode=require`.
- `AUTH_JWKS_URL` / `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE` (`cypherx-platform`) / `AUTH_SERVICE_URL` — inbound JWT verify + service-token mint + agent cross-validation.
- `LLMS_GATEWAY_URL`, `GUARDRAILS_SERVICE_URL` — first-cycle downstreams. `RAG_SERVICE_URL`, `MEMORY_SERVICE_URL`, `TOOL_REGISTRY_URL`, `SKILL_REGISTRY_URL` — WP12/Phase-8 enhancement deps (wired even though stages default-off). Tool-loop knobs: `TOOL_LOOP_MAX_OFFERED_TOOLS` (8), `TOOL_LOOP_TOOL_USE_NUDGE` (true), `TOOL_LOOP_TOOL_MODE` (auto|native|emulated; default unset→gateway auto).
- `VALKEY_URL` (soft), `KAFKA_BROKERS`.
- Toggles/knobs: `STAGE_ENABLE_<NAME>` (per-stage; default ON for LOAD/PRE_GUARDRAIL/PROMPT_BUILD/LLM/POST_GUARDRAIL, OFF for memory/rag/skill/tool), `AGENT_CONFIG_CACHE_*`, `IDEMPOTENCY_*`, `AUTHORIZE_*` (action `task:execute`), `SWEEPER_*`, `REVOCATION_*` (`REVOCATION_KEY_PREFIX=cypherx:rev:`), `ASYNC_MODE_ENABLED`, `SSE_STREAMING_ENABLED`, `TASK_TIMEOUT_SECONDS` (120), prompt-budget / tool-loop bounds, `OTEL_EXPORTER_OTLP_*` (export opt-in; empty endpoint = NO-OP).
- Test-only master flags (default ON in prod): `DB_POOL_OPEN_AT_STARTUP`, `OUTBOX_PUBLISHER_ENABLED` — turned OFF under test to avoid wedging background workers.

## Contracts & cross-repo dependencies
- **Consumes/implements** (`../../contracts`): Contract 1 JWT verify (`jwt/`, RS256 JWKS), Contract 2 error envelope (`api/error-format.schema.json`), Contract 3 A2A request/response (the *request* is a loose public body — FIX 4; only the *response* conforms to `a2a/task-response.schema.json`), Contract 5 Kafka envelope (`schema_version 1.0.0`, `partition_key=tenant_id`), Contract 7 health, Contract 8 W3C trace (traceparent+tracestate+X-Request-ID), Contract 9 idempotency (Idempotency-Key header), Contract 12 service-token + `X-Forwarded-Agent-JWT`, Contract 13 tenant/RLS.
- **Calls downstream** (identity in HEADERS only — forwarded agent JWT + xAgent service token + trace headers; bodies carry NO identity): Auth (token mint, `/v1/authorize`, `GET /v1/agents/{id}`), Guardrails (`/v1/check/input|output`), LLMs gateway (`/v1/chat/completions`); WP12-only (default-off): RAG (`/v1/kbs/{id}/query`), Memory (`/v1/memories[/search]`), Tool-Registry (`/v1/tools[/{name}]`), MCP tool servers (`{invoke_url}/mcp/v1/invoke`).
- **Called by**: external clients via Kong/edge (task submit); the frontend Task Feed (`GET /v1/tasks` list).
- **Kafka produced**: `cypherx.agent.task.completed`, `cypherx.agent.task.failed` (`contracts/kafka/events/agent.task.*.schema.json`); WP12 `cypherx.agent.tools.invocation.metered`. DLQ suffix `.dlq` after 10 attempts. Consumes none.
- **DB schema owned**: `xagent` — `agents`, `tasks`, `task_steps` (RLS tenant-scoped) + `outbox` (NO RLS, internal cross-tenant publish queue). Role `xagent_user` (LOGIN, no BYPASSRLS).

## Invariants & guards (do NOT break)
- **Identity from JWT only** (Contract 13): `tenant_id` / `agent_id` come from the verified token, never the request body or metadata (reserved-key guard rejects `tenant_id|trace_id|task_id|user_id|...` → 422).
- **Caller-vs-target rule** (amended live-bug fix #6): `body.agent_id` MUST equal the JWT `agent_id`; an api_key-only token (no agent identity) and any mismatch → 422 `VALIDATION_ERROR`. Cross-agent invocation is 9B A2A only.
- **Pipeline is a stage registry, not a procedure** — add capabilities as new stages; never re-architect into a hard-coded function. Enhancement slots stay `enabled=False`/skippable so the runtime never errors when later phases slip.
- **EVENT always runs last** (finally-style, even on short-circuit/timeout) — finalises the task row + emits exactly one terminal Kafka event **atomically** in one tenant tx (outbox). Row and event can never diverge. The sweeper-finalize UPDATE is guarded to `status IN ('pending','running')` so it never clobbers an in-process finalize.
- **Per-stage step write-through is fail-soft** — a step-write failure never fails the task; EVENT backstops un-persisted rows and never double-inserts (`StepRow.persisted`).
- **Fail-open vs fail-closed (deliberate):** live token-revocation mirror = **fail-open** (Valkey outage accepts token); agent-config cache & authorize unexpected errors = **fail-open**; guardrails invalid/empty decision & unknown decision = **fail-closed** (treated as block/error, never allow); idempotency on a CONFIGURED-but-erroring Valkey = **fail-closed** (503); cancel with no/erroring Valkey = **503** (cannot guarantee). Idempotency-Key is REQUIRED for `mode=async` (missing → 422).
- **`outbox` has NO RLS** by design — isolation lives in the payload. The sweeper sees RLS'd rows only inside a tx that sets `app.sweeper='on'` (additive OR-combined policy; never grant BYPASSRLS to the role).
- **Failed-event payload field is `error_message`** (FIX 1) even though the DB column is `error_msg`. **Step status `redacted` → `passed`** in the A2A response (FIX 2; `redacted` kept only in the audit row); response always includes `schema_version` + `started_at` + `cost_usd` + `task_steps` (FIX 3).
- **Readiness gates Postgres + Auth JWKS ONLY** — never Valkey/Kafka/downstreams.
- Error→status: `GUARDRAIL_VIOLATION` and `VALIDATION_ERROR` → **422**; `CONFLICT`→409 (agent unconfigured/not active, idempotency in-flight, bad status transition); `TOKEN_REVOKED`→401; `BUDGET_EXCEEDED`→402; `SERVICE_UNAVAILABLE`→503.
- `user_id` is set ONLY from an explicit `user_id` claim — the JWT-`sub` fallback was removed (do not re-add).
- LLM `finish_reason` is validated against the known gateway enum; an UNKNOWN value is treated as `stop` with the raw value preserved in the audit step (do not hard-fail it).

## Gotchas & current status
- **WP12 enhancement stages exist but are OFF.** `rag_query`, `memory_retrieve`, `memory_write`, `tool_loop` are bound but `enabled=False`; `skill_load` is registry-listed but unbound. Even when flagged on, each SKIPS unless the agent config opts in (`allowed_kb_ids` / `allowed_tools` / memory_scope). The default served pipeline is exactly LOAD→PRE_GUARDRAIL→PROMPT_BUILD→LLM→POST_GUARDRAIL→EVENT, and PROMPT_BUILD is byte-identical to the first cycle when no enhancement context is present.
- **A successful first-cycle task writes exactly 3 audit rows**: `guardrail_check_input`, `llm_call`, `guardrail_check_output` (LOAD writes none). WP12 adds `rag_query` / `tool_call` / `tool_loop_limit` / `context_truncated` step types (migration 0005 extends the CHECK enum — keep it in sync with `models/task.py STEP_TYPES`).
- **Compose does NOT set `VALKEY_URL` for xagent** — the container falls back to the `redis://localhost:6379/0` default (no Valkey reach inside the cluster), so idempotency/cancel/agent-config-cache/SSE-pubsub/revocation degrade to their fail-open / DB-poll paths. Inject `VALKEY_URL` via Doppler/env to enable them. (Valkey is a SOFT dependency throughout.)
- **Windows dev:** `main.py` forces `WindowsSelectorEventLoopPolicy` (psycopg3 async can't run on the Proactor loop). No-op on Linux/macOS.
- **Tests are fully network-free** — they null the DB pool, disable the outbox publisher/sweeper, and respx-mock downstreams; `faulthandler_timeout=60` aborts a wedged run. Don't introduce real network calls into the test path.
- **No `.env.example` committed** here (the gitignore allowlists it, but the file is absent) — secrets via Doppler; `SERVICE_BOOTSTRAP_SECRET` is the one required value.
- **Session/9B hooks are stubs:** `_register_session` is a fail-soft no-op until a memory client with `register_session` is wired (durable session record = `tasks.session_id`); cross-agent A2A delegation is Phase 9B (not here).
- **`mode` query param vs body `mode`:** the WP12 async opt-in is the `?mode=async` *query* param; the body model's `mode` Literal still only accepts `sync` (async via the body is 422).
- Migrations are Atlas-managed (`atlas migrate apply --env local`); `schema.sql` is the flattened init+seed end-state for `atlas schema apply` / drift detection.
