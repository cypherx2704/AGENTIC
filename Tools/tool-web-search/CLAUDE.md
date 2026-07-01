# CLAUDE.md — tool-web-search (Tools/tool-web-search)

> Stateless CypherX **MCP tool server** (Contract 4) exposing a single `web_search` tool over HTTP+JSON (`POST /mcp/v1/invoke`); xAgent calls it to search the web and get ranked results. Part of the multi-repo CypherX platform — see the root guide at ../../CLAUDE.md and the owning spec `archive/Manoj/phases/phase-07-tools.md` (Phase 07 "Tools", Component 3).

## What this is
Phase 07 deliverable **Component 3 — tool-web-search**: a stateless (NO database) MCP server that searches the web and returns ranked results with snippets, behind the shared CypherX core (dual-mode JWT auth + WP03 revocation mirror, Contract-2 error envelopes, structlog/Contract-6 logging, Contract-7 health/metrics).

**Implementation status: IMPLEMENTED.** Full FastAPI app under `src/tool_web_search/` with `/manifest` (ETag/304), `/mcp/v1/invoke` (dual-scope auth, JSON-Schema input validation, output cap, fail-open rate limit + idempotency), pluggable providers (`mock`/`serpapi`/`brave`), Dockerfile, `pyproject.toml`+`uv.lock`, and a pytest suite (9 test modules). Default boot is fully keyless (`SEARCH_PROVIDER=mock`, deterministic, no network). (Note: the repo's older `README`/`REPO_ANALYSIS_2026-06-11.md` and earlier CLAUDE.md described a stub — that is stale; trust the source.)

## Tech stack
- **Language/runtime:** Python ≥ 3.12 (asyncio).
- **Web:** FastAPI ≥ 0.115 + uvicorn[standard] (Starlette ASGI; pure-ASGI middleware for trace + a `BaseHTTPMiddleware` body guard).
- **Build/deps:** `uv` (hatchling build backend, package `src/tool_web_search`).
- **Libraries actually used:** pydantic ≥ 2.9 + pydantic-settings (config), PyJWT[crypto] (RS256 JWKS verify via `PyJWKClient`), httpx (real providers), `redis` ≥ 5 (`redis.asyncio` Valkey client), structlog (JSON logs), prometheus-client (metrics). Dev: ruff, pytest, pytest-asyncio, asgi-lifespan, respx.
- **No DB driver, no Kafka client** — stateless tool.

## Repository layout
| Path | Contents |
|------|----------|
| `src/tool_web_search/main.py` | App factory + lifespan (wires Valkey, warms JWKS, installs middleware/handlers/routers). |
| `src/tool_web_search/__main__.py` | `python -m tool_web_search` run entry (Windows SelectorEventLoop handling). |
| `src/tool_web_search/api/` | Routers: `health.py` (`/livez` `/readyz` `/metrics`), `manifest.py` (`/manifest` + ETag/304), `invoke.py` (`POST /mcp/v1/invoke`). |
| `src/tool_web_search/core/` | `config.py` (Settings), `auth.py` (dual-mode JWT + revocation mirror), `errors.py` (Contract-2 envelope), `logging.py`, `metrics.py`, `trace.py`, `body_limit.py`, `valkey.py`. |
| `src/tool_web_search/services/` | `manifest.py` (manifest build + ETag + JSON-Schema validation), `rate_limit.py`, `idempotency.py`, `providers/` (`base.py`, `mock.py`, `serpapi.py`, `brave.py`, `__init__.get_provider`). |
| `tests/` | `conftest.py` (FakeValkey/DownValkey, principal/client fixtures) + `test_auth/health/idempotency/invoke/manifest/output_cap/providers/rate_limit.py`. |
| `Dockerfile` | Multi-stage uv build, non-root uid 10001, listens 8080. |
| `pyproject.toml`, `uv.lock`, `.env.example`, `.dockerignore` | Manifests + example env. |

No `db/migrations/` — this tool owns no schema.

## Build, test, run
**Host (uv):**
```bash
uv venv && uv sync
python -m tool_web_search          # serves on PORT (default 8000 on host)
uv run pytest -q                   # full suite (mock provider, no network/Valkey/Auth)
uv run ruff check .
```
**Docker / compose (canonical):**
```bash
docker build -t cypherx/tool-web-search .
# from umbrella root:
docker compose -f infra/compose/docker-compose.yml up -d tool-web-search
```
- In-container port **8080** (`PORT=8080` in Dockerfile/compose); host map **8091 → 8080** (8090 is the demo).
- `depends_on`: `valkey` (healthy) + `auth-service` (healthy) — but Valkey is soft at runtime.
- **Health/metrics:** `GET /livez` (process-only liveness; also the Docker HEALTHCHECK via stdlib urllib), `GET /readyz` (always `ready:true`; reports `checks.valkey: ok|unavailable`, never gates), `GET /metrics` (Prometheus). `GET /manifest` (ETag/304). `POST /mcp/v1/invoke`.
- Note: Dockerfile/compose set `PORT=8080`, but `__main__.py` defaults to **8000** when `PORT` is unset (host dev).

## Configuration & secrets
All env read directly (pydantic-settings, **no prefix**, case-insensitive, `.env` supported). Doppler injects in cloud; only `.env.example` is committed.
- `SERVICE_NAME`, `SERVICE_VERSION` (0.1.0), `ENVIRONMENT`, `HOST`, `PORT`.
- `SEARCH_PROVIDER` = `mock` (default, deterministic/keyless/no network) | `serpapi` | `brave`; `SERPAPI_API_KEY`, `BRAVE_API_KEY` (blank in mock), `PROVIDER_TIMEOUT_SECONDS=10.0`, `DEFAULT_MAX_RESULTS=5`, `MAX_MAX_RESULTS=20`.
- `VALKEY_URL` (`redis://valkey:6379` in compose) — **soft** dep for rate limit + idempotency; `VALKEY_PING_TIMEOUT_SECONDS=2.0`.
- `AUTH_JWKS_URL`, `AUTH_ISSUER_URL`, `AUTH_PLATFORM_AUDIENCE=cypherx-platform` (Contract 1; 5-min JWKS cache).
- `REVOCATION_CHECK_ENABLED=true`, `REVOCATION_KEY_PREFIX=cypherx:rev:` (must match Auth), `REVOCATION_VALKEY_TIMEOUT_SECONDS=0.15`.
- `MANIFEST_SCHEMA_VERSION=1.0.0`, `MANIFEST_PROTOCOL_VERSION=mcp/1.0`, `TOOL_TIMEOUT_SECONDS=30`.
- `MAX_OUTPUT_BYTES=10485760` (10 MiB result cap), `MAX_REQUEST_BODY_BYTES=1048576` (1 MiB body cap).
- Rate limit: `RATE_LIMIT_ENABLED=true`, `RATE_LIMIT_REQUESTS_PER_MIN=60`, `RATE_LIMIT_KEY_PREFIX=cypherx:tws:rl:`, `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_VALKEY_TIMEOUT_SECONDS=0.15`.
- Idempotency: `IDEMPOTENCY_ENABLED=true`, `IDEMPOTENCY_KEY_PREFIX=cypherx:tws:idem:`, `IDEMPOTENCY_TTL_SECONDS=86400` (24h), `IDEMPOTENCY_VALKEY_TIMEOUT_SECONDS=0.15`.

## Contracts & cross-repo dependencies
- **Implements:** Contract 4 (MCP manifest validates against `contracts/mcp/manifest.schema.json`; server dash-case `tool-web-search`, tool snake_case `web_search`), Contract 1/12/13 (dual-mode JWT, identity from JWT only), Contract 2 (error envelope), Contract 6 (structlog JSON), Contract 7 (`/livez` `/readyz` `/metrics`), Contract 8 (`traceparent` / `X-Request-ID`), Contract 9-style idempotency, WP03 revocation mirror.
- **Manifest tool `web_search`:** `input_schema` `{ query: string (minLength 1, required), max_results?: integer 1..MAX_MAX_RESULTS, default 5 }`; `output_schema` `{ results: [{ title, url, snippet, rank }] }`; `timeout_seconds=30`, `idempotent=true`, `rate_limit.rpm=60`. `required_scopes=[tool:invoke, tool:tool-web-search:invoke]`. `invoke_endpoint=/mcp/v1/invoke`.
- **Called by:** xAgent (invokes `/mcp/v1/invoke`); tool-registry (polls `/manifest` with `If-None-Match` for 304, health-polls `/livez`, and seeds `tool-web-search@1.0.0`). Calls **auth-service** (JWKS) and **Valkey** only.
- **Kafka:** produces/consumes NOTHING. Per-invocation metering is emitted from xAgent's outbox — do NOT add a tool-side emitter.
- **DB:** none (registry catalogue lives in `registry.*` owned by tool-registry).

## Invariants & guards (do NOT break)
- **Stateless — no DB, no Kafka, no outbox.** Never add any of these.
- **Identity comes ONLY from the JWT, never the body** (Contract 13). `tenant_id`/`agent_id` are read from claims; the invoke body carries only `tool`/`args`.
- **Dual-mode auth + dual scope.** EXTERNAL = bare agent/api-key JWT; INTERNAL = `svc:`/`svc-ext:` service token + `X-Forwarded-Agent-JWT` where the service token's `on_behalf_of` MUST equal the forwarded agent's `agent_id` (401 mismatch). `iss`/`aud`/`exp`/`sub` required; `aud` accepts `cypherx-platform` OR `*`; RS256 only. Coarse `tool:invoke` checked in `require_principal` (403); fine `tool:tool-web-search:invoke` checked in the invoke handler (403).
- **WP03 revocation mirror is FAIL-OPEN.** Checks `jti`/`kid`/agent-epoch in shared Valkey AFTER signature/scope; Valkey down → ACCEPT token (log `revocation_check_skipped`, bump metric). A genuine match → 401 `TOKEN_REVOKED`.
- **Rate limiter + idempotency are FAIL-OPEN.** Any Valkey problem → ALLOW / no-replay; never fail `/readyz`. Over limit → 429 `RATE_LIMIT_EXCEEDED` + `Retry-After`.
- **`/livez` never touches Valkey/providers; `/readyz` never gates on Valkey** (soft dep, reported only).
- **Server-side input validation** against the manifest `input_schema` on every invoke (a dependency-free validator for type/required/minLength/minimum/maximum/additionalProperties); failure → 422 `VALIDATION_ERROR` with `details.pointer` = JSON Pointer (e.g. `/query`). `bool` is rejected as a non-integer.
- **10 MiB serialized output cap** → 413 `PAYLOAD_TOO_LARGE` (`details.reason=OUTPUT_BYTES_EXCEEDED`); **1 MiB request body cap** (Content-Length) → 413 (`BODY_BYTES_EXCEEDED`).
- **Idempotency** keyed `(prefix, tenant_id, Idempotency-Key)`, TTL 24h; replay sets header `Idempotency-Replayed: true`.
- **Error codes use Contract-2 spelling** (`IDEMPOTENCY_*`, not `IDEMPOTENT_*`); every error renders the `{ error: { code, message, details?, request_id, trace_id, timestamp } }` envelope (the body-limit middleware emits the same shape directly since it runs outside the handler stack).
- **mock provider must stay deterministic** (the test suite asserts exact bodies; first cycle is keyless). The `__bloat__:<n>` query is a reserved output-cap test seam — don't repurpose it.

## Gotchas & current status
- **Spec vs. code wire shape:** the phase-07 spec example shows `{ "tool": "web_search", "input": {...} }`, but the implemented invoke body uses **`args`** (with `arguments` as an alias). `tool` is optional (single tool); if present it must equal `web_search` (else 404 `NOT_FOUND`). No-wrapper bodies treat top-level keys (minus `tool`/`args`/`arguments`) as the args.
- `__main__.py` host default port is **8000** (compose/Dockerfile override to 8080) — easy to trip on host dev.
- Windows: both `main.py` (import-time) and `__main__.py` set `WindowsSelectorEventLoopPolicy` and run uvicorn with `loop="none"` on win32; no-op on Linux/macOS. (The psycopg3 comment is copied from SharedCore boilerplate — there is no DB here.)
- Body guard only enforces on **Content-Length**; a chunked-and-lying client is bounded by uvicorn's own ceiling (intentional — wrapping ASGI receive in `BaseHTTPMiddleware` corrupts body parsing).
- Real providers (`serpapi`/`brave`) construct lazily and raise `ProviderError` if their key is missing; provider errors → 502, timeout → 504 (`SERVICE_UNAVAILABLE`). Tests drive them via respx, never the live network.
- `valkey.set_if_absent` exists but is currently unused (idempotency uses `set`/`get`). `REVOCATION_VALKEY_TIMEOUT_SECONDS` is read from env but is not a typed Settings field (verify before relying on a non-default).
- `REPO_ANALYSIS_2026-06-11.md` and the stub-era CLAUDE.md predate this implementation — treat the source tree as ground truth.
