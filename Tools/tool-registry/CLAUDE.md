# CLAUDE.md — tool-registry (Tools/tool-registry)

> CypherX **Tool Registry** (Phase 07 / WP11): the FastAPI catalogue of MCP tool servers. Agents discover platform + per-tenant tools, resolve `name@version` → a versioned manifest + invoke URL + required scopes, and read each tool's live health (background manifest poll). Owns the Postgres `tools` schema as role `tool_user`. Platform root guide: [../../CLAUDE.md](../../CLAUDE.md).

## What this is
The Tool Registry service — Phase 07 "Tools (MCP Servers)", Component 1 (WP11, part 1). It stores the tool/version registry, resolves tenant-vs-platform discovery (tenant-priority shadowing + version pinning), validates and persists Contract-4 MCP manifests, declares each tool's capabilities/scopes, and tracks tool health via a 30s background manifest poll.

**Implementation status: IMPLEMENTED** on `development` (was previously a stub). Full async FastAPI app with discovery + registration APIs, dual-mode JWT auth + WP03 revocation, RLS-scoped psycopg3 data layer, two SQL migrations (schema + seed), a Dockerfile, and ~12 test modules. (NOTE: the committed text in the prior `CLAUDE.md` describing a `registry.*` schema and stub status is OUT OF DATE — the real schema is `tools.*` and the service is built.)

## Tech stack
- **Python 3.12**, **FastAPI** + **uvicorn[standard]**, **pydantic v2** / **pydantic-settings**.
- **psycopg 3** (async, `[binary,pool]` — bundles libpq) with `psycopg_pool.AsyncConnectionPool`.
- **PyJWT[crypto]** (RS256 + `PyJWKClient` JWKS cache), **redis** (`redis.asyncio` → Valkey), **httpx** (manifest polling), **structlog**, **prometheus-client**.
- Build tool **uv** (`uv sync --frozen`, `uv.lock`); packaging via **hatchling** (`packages = ["src/tool_registry"]`). Lint: **ruff**. Tests: **pytest** + **pytest-asyncio** (`asyncio_mode=auto`) + **asgi-lifespan** + **respx**.

## Repository layout
| Path | Contents |
|------|----------|
| `src/tool_registry/main.py` | App factory + lifespan (DB pool, Valkey, httpx client, seed, JWKS warm, health-poll task). |
| `src/tool_registry/__main__.py` | `python -m tool_registry` entry; Windows SelectorEventLoop fix; reads `HOST`/`PORT`. |
| `src/tool_registry/api/health.py` | `/livez`, `/readyz`, `/metrics` (Contract 7). |
| `src/tool_registry/api/tools.py` | `GET /v1/tools`, `GET /v1/tools/{name}`, `POST /v1/tools`, `POST /v1/tools/{name}/versions`. |
| `src/tool_registry/core/auth.py` | Dual-mode JWT verify (Contracts 1/12/13) + WP03 revocation mirror; `Principal`, `require_principal`, `require_scopes`. |
| `src/tool_registry/core/{config,errors,logging,metrics,trace}.py` | Settings; Contract-2 error envelope; structlog; Prometheus; W3C trace middleware. |
| `src/tool_registry/db/{pool,queries,valkey}.py` | RLS tx helpers (`in_tenant`/`in_platform`); all SQL; lazy Valkey client. |
| `src/tool_registry/services/{discovery,manifest,seed,health_poll,health_runner}.py` | Shadowing/invoke-URL; Contract-4 validation; platform seed; health state machine + poll loop. |
| `db/migrations/20260611_0001__init.sql` | `tools` schema, 4 tables, indexes, split RLS + grants. |
| `db/migrations/20260611_0002__seed.sql` | Platform `tool-web-search` tool + version + capability + health rows. |
| `tests/` | `conftest.py`, `fakes.py`, ~12 modules (auth, RLS cross-tenant, discovery, registration, manifest validation, health state machine + poll, seed, health endpoints). |
| `Dockerfile`, `pyproject.toml`, `uv.lock`, `.env.example` | Image, manifests, env template. |

## Build, test, run
**Host (uv):**
```bash
uv sync                                   # install (dev group included)
./.venv/Scripts/python.exe -m pytest -q   # tests need NO live DB/Valkey/Auth (fakes + db_pool=None degradation)
./.venv/Scripts/python.exe -m tool_registry   # serves on HOST/PORT (PORT defaults to 8000 here; .env can override)
```
**Docker / infra/compose** (`infra/compose/docker-compose.yml`, service `tool-registry`):
- Build context `../../Tools/tool-registry`, image `cypherx/tool-registry:local`, container `cypherx-tool-registry`.
- In-container port **8080** (`PORT=8080`); host map **8089:8080**. CMD `python -m tool_registry`, non-root uid 10001.
- `depends_on`: `valkey` (healthy) + `auth-service` (healthy). xAgent in turn depends on `tool-registry` healthy.
- **Health (Contract 7):** `GET /livez` (process-only; Docker/compose healthcheck via stdlib urllib), `GET /readyz` (gated on Postgres only — Valkey is soft), `GET /metrics` (Prometheus 0.0.4).
- **Migrations:** applied by the compose `--profile migrate` one-shot, which mounts `db/migrations` → `/migrations/tool-registry` and runs against the Neon **DIRECT** endpoint (superuser; also provisions role `tool_user`). Files are idempotent; can also be psql'd in order.

## Configuration & secrets
All env, no prefix (`core/config.py`, pydantic-settings); real values in **Doppler**, only `.env.example` committed.
- `DATABASE_URL` — Postgres DSN, role `tool_user`, schema `tools` (compose: `${TOOL_REGISTRY_DATABASE_URL}`, Neon **POOLED**).
- `VALKEY_URL` (default `redis://valkey:6379`), `VALKEY_PING_TIMEOUT_SECONDS=2.0` — **soft** dependency.
- `AUTH_JWKS_URL`, `AUTH_ISSUER_URL`, `AUTH_PLATFORM_AUDIENCE=cypherx-platform` — JWT verify (Contract 1).
- `REVOCATION_CHECK_ENABLED=true`, `REVOCATION_KEY_PREFIX=cypherx:rev:` (MUST match Auth), `REVOCATION_VALKEY_TIMEOUT_SECONDS=0.15` — WP03 fail-open mirror.
- `MAX_ACTIVE_VERSIONS_PER_TOOL=3` — version retention cap (oldest retired).
- `HEALTH_POLL_INTERVAL_SECONDS=30`, `HEALTH_POLL_TIMEOUT_SECONDS=5`, `HEALTH_DEGRADE_AFTER=1`, `HEALTH_OFFLINE_AFTER=3` — poll + state machine.
- `SEED_PLATFORM_TOOLS=true`, `TOOL_WEB_SEARCH_BASE_URL=http://tool-web-search:8080` — startup seed of the platform web-search tool (base_url NEVER hardcoded).
- `DISCOVERY_MAX_TOOLS=500` — hard row cap on `GET /v1/tools`.
- `HOST`/`PORT`, `ENVIRONMENT`, `SERVICE_NAME`/`SERVICE_VERSION`, `OTEL_EXPORTER_OTLP_ENDPOINT` (optional).
- **Keyless local:** the registry calls no LLM/provider APIs; mock toggles are irrelevant here (only the sibling `tool-web-search` uses `SEARCH_PROVIDER=mock`).

## Contracts & cross-repo dependencies
Source of truth is `contracts/` — honour it, never edit contracts to match code.
- **Contract 4 (MCP manifest):** `services/manifest.py` validates required top-level fields + formats (dash-case server `name`, semver `version`/`schema_version`, `mcp/x.y` `protocol_version`, non-empty `tools[]` with snake_case names + `input_schema`). Deliberately NOT a full JSON-Schema engine (`additionalProperties: true`).
- **Contract 1/12/13 (auth):** RS256 JWKS verify; `iss`==issuer, `aud` ∈ {platform audience, `*`}; **EXTERNAL** (bare agent/api-key JWT) and **INTERNAL** (`svc:`/`svc-ext:` service token + `X-Forwarded-Agent-JWT` whose `agent_id` must equal the service token's `on_behalf_of`) modes. `tenant_id`/`agent_id` taken ONLY from the JWT.
- **WP03 revocation:** verifier-side Valkey mirror (`jti` / `kid` / agent-epoch keys under shared prefix), checked AFTER signature; **fails open** if Valkey is down.
- **Contract 2 (errors):** `{ error: { code, message, details?, request_id, trace_id, timestamp } }`; codes in `core/errors.py`.
- **Contract 7 (health):** `/livez`, `/readyz` (Postgres-gated), `/metrics`.
- **Contracts 6/8 (trace/logs):** `TraceContextMiddleware` parses `traceparent`/`X-Request-ID`/`X-Tenant-ID`/`X-Agent-ID` and binds structlog contextvars; echoes `X-Request-ID`.
- **DB owned:** schema `tools` (Contract 14 single-owner) — tables `tools`, `tool_versions`, `tool_capabilities`, `tool_health`; runtime role `tool_user` (LOGIN, NOT superuser, NOT BYPASSRLS).
- **Callers/callees:** called by **xAgent** (discovery); calls **auth-service** (JWKS) and each tool server's `GET {base_url}/manifest` (e.g. **tool-web-search**). **No Kafka** — the registry emits no events (tool-invocation metering is emitted by xAgent's outbox).

## Invariants & guards (do NOT break)
- **Marketplace-hole RLS (security):** every tenant-scoped table has a SPLIT policy — `*_read` (`FOR SELECT USING own OR platform`) + `*_write` (`FOR ALL` with **`WITH CHECK (tenant_id = current_tenant)`**) + `*_platform` (empty-GUC, `tenant_id IS NULL`). The `WITH CHECK` half rejects writing a row with another tenant's id or `NULL` (forging a platform tool). Applies to **every** table **including `tool_capabilities`** (RLS does NOT propagate across joins).
- `tool_health` additionally has a `*_poller` policy so the empty-GUC sweep can update health for ALL tools (incl. tenant-owned) — but only from the trusted empty-GUC context.
- Every tenant query MUST run inside `in_tenant()` (`SET LOCAL app.tenant_id`); poller/seed use `in_platform()` (empty GUC). All predicates use `NULLIF(current_setting('app.tenant_id',true),'')::uuid` (pooled-reset safe — never bare `''::uuid`).
- **`tenant_id`/`agent_id` come from the JWT only**, never the request body (Contract 13).
- **Version retention:** max `MAX_ACTIVE_VERSIONS_PER_TOOL` (default 3) active versions; on overflow the OLDEST active is set `status='retired'`. `latest_version` advances on each new version.
- **Tenant shadows platform:** for a name collision, the tenant's tool hides the platform tool (a tenant can never see another tenant's tool — RLS).
- **Invoke URL:** manifest `base_url` (stripped) else `http://<name>:8080`; never hardcode cluster DNS. Discovery returns the resolved versioned manifest.
- **Health poll is ETag-aware + fail-soft:** `If-None-Match` → 200 = changed (cache manifest+ETag), 304 = unchanged (preserve ETag), error/timeout/non-2xx = failure. A poll error never escapes the loop; a freshly-registered tool is polled EAGERLY (best-effort).
- **Valkey is SOFT:** `/readyz` reports it but never gates on it; readiness = Postgres only.
- Manifest unique constraints: `(tenant_id, name)` per tenant, partial unique `name WHERE tenant_id IS NULL` for platform; `(tool_id, version)`; `(tool_id, capability)`. Duplicate → 409 CONFLICT.
- Registration requires scope `tool:admin` OR `platform:admin`; discovery only needs an authenticated principal.

## Gotchas & current status
- **Stale committed `CLAUDE.md`:** the existing file still describes a stub with a `registry.*` schema and Atlas/Kong details — it does NOT match the implemented code. The real schema is `tools.*`; trust the source files.
- **Windows dev:** psycopg3 async cannot run on the default ProactorEventLoop; `main.py` and `__main__.py` force `WindowsSelectorEventLoopPolicy` (no-op on Linux/macOS). `__main__.py` runs uvicorn with `loop="none"` on win32 so uvicorn doesn't re-select Proactor.
- **PORT default differs by entrypoint:** `__main__.py` defaults `PORT` to **8000**; the Dockerfile/compose set `PORT=8080`. `.env.example` shows port-8080 service URLs.
- **Boot is fail-soft:** DB pool opens with `wait=False`; seed is `asyncio.wait_for(..., 4.0)` and swallows errors; JWKS warm is best-effort. The service starts even if Postgres/Valkey/Auth are down (readyz reflects DB).
- **Manifest validation is intentionally partial** (required fields + formats only) — forward-compatible with `additionalProperties: true`; do not add a heavyweight schema validator without reason.
- Tests need no live infra: `conftest.py` pins a harmless `DATABASE_URL`, sets `SEED_PLATFORM_TOOLS=false`, and relies on the `db_pool=None` graceful degradation + injected fakes (`tests/fakes.py`). `Settings` is `lru_cache`d — first caller wins per process.
- **Capabilities are replaced on every version write** (`DELETE` then re-insert from the latest manifest), so they always reflect the newest active manifest.
