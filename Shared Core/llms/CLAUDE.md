# CLAUDE.md — llms-gateway (Shared Core/llms)

> Unified, provider-normalized LLM gateway (CypherX Phase 03): the single choke point every CypherX
> service routes ALL LLM chat + embeddings traffic through — translating one OpenAI-superset schema
> to/from Anthropic & OpenAI, metering tokens/cost, and emitting billing events. Platform-wide rules:
> [../CLAUDE.md](../CLAUDE.md). Owning spec: `archive/Manoj/phases/phase-03-llms.md`.

## What this is

A **fully-implemented** Python 3.12 / FastAPI service (~6,600 LOC under `src/`), built with `uv`. It is
the platform's unified LLM gateway — no other service calls a provider SDK directly. It implements the
Phase-03 critical-path spine plus WP05 (rate-limit/idempotency) and WP06 (embeddings, BYOK, per-key
ACLs, SSRF image fetcher, pricing-staleness watchdog). Surfaces: `POST /v1/chat/completions`
(streaming + non-streaming), `POST /v1/embeddings`, `POST /v1/rerank` (pluggable reranker, default
deterministic mock; cross-encoder seam behind `RERANK_PROVIDER=local`), `POST /v1/classify` (safety
classifier, default `CLASSIFIER_MODE=stub`; small-safety-model seam behind `CLASSIFIER_MODE=local`),
BYOK key CRUD `/v1/keys`, read APIs `/v1/models | /v1/usage | /v1/cost`, and `/livez | /readyz | /metrics`.
The rerank/classify surfaces wire through the SAME auth/ACL/idempotency/rate-limit/metering path as
chat & embeddings; both default to keyless deterministic providers and meter by UNITS (no cost rewrite).
An `eval/` harness (golden set + NDCG/MRR + verdict-accuracy runner) makes the mock→real improvement measurable.

> ⚠️ The `README.md` line "Deferred (not yet built): BYOK and the SSRF-hardened multimodal fetcher" is
> **stale**, and `db/migrations/README.md` only documents migrations through `_0005`. Both BYOK
> (`services/byok.py`, `api/keys.py`, migration `..._0006`, `test_wp06_byok.py`) and the SSRF fetcher
> (`services/image_fetch.py`, `test_wp06_image_fetch_ssrf.py`), plus ACLs (`..._0007`), are built +
> tested. Trust the code/tests over those two docs.

## Tech stack

FastAPI + Uvicorn, Pydantic v2 / pydantic-settings, psycopg3 async pool (`psycopg[binary,pool]`),
aiokafka, `redis` (Valkey client), `anthropic` + `openai` SDKs, PyJWT[crypto], `cryptography` (BYOK
AES-256-GCM + HKDF), structlog, prometheus-client. Build/run via `uv`; lint `ruff`, types `mypy`, tests
`pytest` + `pytest-asyncio`. Migrations follow the **Atlas** convention (PostgreSQL 16). Package import
root: `llms_gateway` (in `src/`); container entry `python -m llms_gateway`.

## Repository layout

| Path | Holds |
|------|-------|
| `src/llms_gateway/main.py` | App factory + lifespan (DB pool, registry warm + 60s refresh loop, lazy Valkey, outbox publisher, JWKS warm, billing-replay + pricing-staleness drivers). |
| `src/llms_gateway/__main__.py` | `python -m llms_gateway` entry (Windows SelectorEventLoop fix; PORT default **8000**, HOST 0.0.0.0). |
| `src/llms_gateway/api/` | Routers: `chat.py`, `embeddings.py`, `read.py` (models/usage/cost), `keys.py` (BYOK CRUD), `health.py`. |
| `src/llms_gateway/core/` | `auth.py` (dual-mode JWT + revocation mirror), `errors.py` (Contract-2 envelope), `config.py` (Settings), `trace.py` (Contract 6/8 middleware), `body_limit.py`, `logging.py`, `metrics.py`. |
| `src/llms_gateway/models/unified.py` | OpenAI-superset chat + embeddings request/response models (tools, multimodal `image_url`, reserved-`metadata` guard). |
| `src/llms_gateway/services/` | `router.py` (alias→provider + BYOK key selection), `normalizer.py` (Anthropic/OpenAI translate), `tool_emulation.py` (**tool-calling shim** for non-native/small models — see note), `cost.py`, `capabilities.py`, `rate_limit.py`, `idempotency.py`, `acl.py`, `byok.py`, `image_fetch.py`, `auth_client.py` (plan→limits), `billing_journal.py`, `pricing_staleness.py`, `providers/{base,mock,anthropic_provider,openai_provider}.py`. |

> **Tool-calling emulation (small/8B models, 2026-06-24).** `model_capabilities.native_tool_use=false`
> (or request `tool_mode=emulated`) routes a `tools` request through `services/tool_emulation.py`:
> the tool schemas + a strict tool-call protocol are injected into the prompt, the provider is
> called as a plain chat, and the model's text reply is parsed back into normalized
> `message.tool_calls` + `finish_reason=tool_calls`. `tool_mode` (auto|native|emulated, default
> auto) is on `ChatCompletionRequest`; the response carries `X-Cypherx-Tool-Mode`. Seeded small
> models: `llama-3.1-8b-instruct`/`qwen2.5-7b-instruct`/`mistral-7b-instruct` (alias `small`).
| `src/llms_gateway/db/` | `pool.py` (`in_tenant()` RLS helper + platform reads + `readyz_ping`), `outbox.py` (usage + 2 events txn + publisher), `read_queries.py`, `valkey.py`. |
| `db/migrations/` | Atlas SQL (`..._0001`→`..._0007`), `schema.sql` flattened snapshot, `atlas.hcl`, `README.md`. |
| `tests/` | 24 test files (normalizer, chat/stream mock, WP05/WP06 suites, outbox payloads, auth, config-registry drift guard). |
| `Dockerfile` | Multi-stage uv build; non-root uid 10001; `EXPOSE 8080`; CMD `python -m llms_gateway`. |

## Build, test, run

```bash
python -m uv sync                       # install deps
# Local keyless run (NO keys / DB / Kafka required) — Uvicorn on :8000
MOCK_PROVIDERS=true python -m uv run uvicorn llms_gateway.main:app --reload
curl -s localhost:8000/livez
python -m uv run pytest                 # tests run with no Auth/DB/Kafka (mock provider, auth override, DB pool absent)
python -m uv run ruff check src tests && python -m uv run mypy
```

In-container entry is `python -m llms_gateway`, honoring `PORT` (Dockerfile sets `PORT=8080`,
`HOST=0.0.0.0`). **Canonical in-container port 8080; host port 8085** via
`infra/compose/docker-compose.yml` (service `llms-gateway`, host map `8085:8080`, in-cluster URL
`http://llms-gateway:8080`, `MOCK_PROVIDERS` defaults `true`). Health: `GET /livez` (process-only
liveness, never touches deps), `GET /readyz` (gated ONLY on Postgres; Valkey reported `ok|unavailable`
but never gates), `GET /metrics` (Prometheus). The Docker HEALTHCHECK hits `/livez` via stdlib urllib
(no curl in the image).

> `__main__.py` defaults `PORT` to **8000**; Dockerfile/compose set `PORT=8080`. Bare
> `python -m llms_gateway` with no env listens on 8000. Honor 8080 in any container/compose context.

DB migrations (separate Atlas tool, **direct** connection): `atlas migrate apply --env local`, or apply
`db/migrations/schema.sql`. Postgres is **external (Neon)** — no postgres container; apps use the Neon
**pooled** endpoint (transaction mode), migrations use the Neon **direct** endpoint; `sslmode=require`
is mandatory for Neon.

## Configuration & secrets

All env-driven (no prefix), via pydantic-settings; see `.env.example` (placeholders only — real secrets
live in **Doppler**; never commit `.env`). Mock toggle: `MOCK_PROVIDERS` (default `false`; `true` =>
deterministic mock provider/embeddings, no keys/network). Key vars:

- `DATABASE_URL` — psycopg DSN (role `llms_user`, schema `llms`; **Neon** pooled at runtime, `sslmode=require`).
- `KAFKA_BROKERS`, `VALKEY_URL` (+ `VALKEY_PING_TIMEOUT_SECONDS`) — both **soft** deps.
- `AUTH_JWKS_URL` / `AUTH_ISSUER_URL` / `AUTH_PLATFORM_AUDIENCE` (Contract 1; point at auth-service).
- `AUTH_BASE_URL` / `DEFAULT_PLAN` / `PLAN_CACHE_TTL_SECONDS` — plan→limits resolution (WP05).
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — platform provider keys (blank in mock mode).
- `LLMS_BYOK_KEK` — master KEK (HKDF-SHA256 → 32 bytes; any passphrase works); **empty disables BYOK**
  (registration refused, resolver returns None, platform key used). `BYOK_GRACE_DAYS` (7).
- `REVOCATION_*` — shared Valkey kill-switch mirror (prefix MUST match Auth's `REVOCATION_KEY_PREFIX`).
- Toggles/caps: `RATE_LIMIT_ENABLED`, `IDEMPOTENCY_ENABLED`, `ACL_ENABLED`, `IMAGE_INLINE_REQUIRED`
  (default `false` = URL pass-through), `MAX_REQUEST_BODY_BYTES` (25 MiB), `MAX_IMAGES_PER_REQUEST` (4),
  `MAX_IMAGE_BYTES` (20 MiB), `EMBEDDINGS_MAX_INPUT_ITEMS` (256), `EMBEDDINGS_MAX_PAYLOAD_BYTES`
  (25 MiB), `MAX_TOKENS_OVER_CAP_POLICY` (`reject`|`clamp`), `STREAM_WALL_CLOCK_TIMEOUT_SECONDS` (120),
  `BILLING_JOURNAL_*`, `PRICING_STALENESS_*`, `CONFIG_REFRESH_INTERVAL_SECONDS` (60).

## Contracts & cross-repo dependencies

- **Consumes `contracts/`:** error envelope (Contract 2), JWT/JWKS (Contract 1/12/13 dual-mode auth;
  identity ONLY from JWT, never body), event envelope (Contract 5), health (Contract 7),
  tracing/correlation (Contract 6/8), idempotency (Contract 9), per-key ACLs (Contract 18),
  usage/metering (Contract 19). Honors contracts; never the reverse.
- **Called by:** xAgent, Guardrails, RAG (embeddings), Memory (embeddings) — all via the gateway.
- **Calls:** Anthropic + OpenAI provider APIs; auth-service JWKS; Valkey; Kafka.
- **Kafka produced (per completion, in ONE txn with the usage row):** `cypherx.llms.request.completed`
  and `cypherx.llms.usage.recorded` (+ `<topic>.dlq` after 10 attempts). Partition key = `tenant_id`;
  Contract-5 envelope; metering `operation` mapped `chat→chat.completion`, `embedding→embedding`.
- **DB owned (schema `llms`, role `llms_user`, non-superuser, RLS-bound):** `usage_records`
  (tenant RLS; UNIQUE `(tenant_id, llm_call_id)`; `operation` `'chat'`|`'embedding'`), `outbox`
  (RLS **disabled** — internal cross-tenant publish queue; `tenant_id` backfilled from `partition_key`
  by trigger), `model_aliases` (mixed-scope RLS, admits `tenant_id IS NULL`), `provider_pricing` /
  `model_capabilities` / `rate_limits` / `providers` / `secret_backends` (platform-scoped, no RLS),
  `tenant_provider_keys` (BYOK, tenant RLS), `api_key_acls` (tenant RLS). Auth owns API keys — this
  repo stores only `api_key_acls` keyed by Auth's `api_key_id` (no second `api_keys` table here).

## Invariants & guards (do NOT break)

- **Neon pooled DSN must NOT carry `options=-c search_path=llms`.** The runtime DSN connects through the
  Neon **pooled** (transaction-mode) endpoint, which does not reliably preserve session startup
  options. ALL runtime SQL is fully schema-qualified (`llms.<table>`) precisely so no `search_path` is
  needed — keep it that way. `search_path=llms` appears ONLY in `db/migrations/atlas.hcl` (the Atlas
  direct-connection migration tool), never in the app DSN. Valkey is likewise a **soft** dep —
  `/readyz` reports it but never gates on it.
- **DB is the single authority** for aliases/pricing/capabilities: loaded at startup + refreshed every
  60s. In-code maps (`_PLATFORM_ALIASES`, `_LITERAL_PROVIDER`, `_FALLBACK_PRICING`) are **cold-start
  fallbacks only**, drift-guarded by `tests/test_config_registry.py` against the seed SQL — keep them
  byte-equal to the seed.
- **`in_tenant()` for every tenant-scoped query** — `llms_user` does not bypass RLS; the helper sets
  `app.tenant_id` transaction-locally (`set_config(..., true)`). Never query tenant tables outside it.
- **Billing never 5xx's the client after the provider charged tokens** — on DB-write failure: log,
  count, journal the `UsageWrite` (NDJSON) for replay, serve `X-Cypherx-Billing-Pending: true`.
- **`llm_call_id` is THE billing uniqueness key** (gateway-minted UUIDv4 per call). `request_id` is a
  non-unique correlation column (one upstream `X-Request-ID` may span multiple calls — both bill). The
  hot-path INSERT uses **NO `ON CONFLICT`** (a dup is a bug → fail loudly); only the replay worker may.
- **Identity from JWT only** (Contract 13): `tenant_id`/`agent_id` from token, never body. `metadata`
  rejects reserved keys (`agent_id,tenant_id,trace_id,span_id,request_id,task_id,user_id,org_id`) →
  400 `VALIDATION_ERROR`. Dual-mode auth (Contract 12): EXTERNAL = bearer agent JWT; INTERNAL = service
  JWT (`sub` starts `svc:`/`svc-ext:`) + `X-Forwarded-Agent-JWT`, where the service token's
  `on_behalf_of` MUST equal the forwarded agent's `agent_id`. `aud` accepts the platform audience OR
  `*`. Revocation mirror (jti/kid/agent-epoch) is checked AFTER sig/iss/aud/exp/scope and **fails open**.
- **Fail-open soft deps:** Valkey (rate-limit, idempotency, revocation, plan-cache) and Kafka outages
  must never block/hard-fail a request — log + metric, proceed. `/readyz` gates ONLY on Postgres.
- **`outbox` RLS stays DISABLED** (the publisher reads `llms.outbox` without `in_tenant`; isolation is
  in the payload + `partition_key`).
- **Anthropic normalizations** (`normalizer.py`): system→top-level `system`; `tool_use`↔`tool_calls`;
  `stop_reason: tool_use→tool_calls`, `refusal→content_filter`; cache tokens from
  `cache_read/creation_input_tokens`; temperature clamp `[0,2]→[0,1]`; `parallel_tool_calls →
  disable_parallel_tool_use`; `response_format json_object/json_schema → 422 MODEL_UNSUPPORTED` (no
  silent fallback). Streaming `stream=true` records the Idempotency-Key but is **replay-exempt**.
- **BYOK secrets never stored raw / never logged** — `secret_ref` is `env:NAME` or `sealed:v1:<b64>`
  (AES-256-GCM DEK wrapped by HKDF-derived KEK; blob = `dek_nonce|wrapped_dek|ct_nonce|ciphertext`).
  Empty `LLMS_BYOK_KEK` => BYOK disabled. Key-management endpoints (`/v1/keys`) require scope
  `tenant:admin` OR `platform:admin` (beyond the gateway-wide `llm:invoke`).
- **SSRF image fetcher is OFF by default** (URL pass-through). Only invoked when
  `IMAGE_INLINE_REQUIRED=true`; then it enforces scheme/IP allow-list, size cap, timeout, `image/*`
  content-type, no redirects — do not weaken these.
- Required scope for chat AND embeddings: **`llm:invoke`**. Canonical replay header spelling is
  `Idempotent-Replayed`; clamp header is `X-Cypherx-Param-Clamped: max_tokens`.

## Gotchas & current status

- **Status: implemented** across WP01–WP06; tests are per-feature (`test_wp05_*`, `test_wp06_*`) and run
  keyless (mock provider, auth dependency overridden, DB pool absent so usage-writes no-op).
- Two docs lag the code: `README.md` calls BYOK + the SSRF fetcher "deferred", and
  `db/migrations/README.md` lists migrations only through `_0005`. Both are stale — BYOK (`_0006`:
  `providers`, `secret_backends`, `tenant_provider_keys`) and ACLs (`_0007`: `api_key_acls`) are present
  + tested. The flattened `schema.sql` may also lag the numbered files; when in doubt apply the numbered
  migrations in order.
- **Plan→limits resolver** (`services/auth_client.py`): primary source is the JWT `plan` claim, then the
  DB `llms.rate_limits` row; the Auth HTTP `/limits` fallback is a documented **stub** until a
  service-token provider exists. Token rate-limiting is **post-hoc debit** (a single request can
  overshoot its own size); only `requests_per_min` is a pre-flight gate.
- Windows: psycopg3 async needs the SelectorEventLoop — set unconditionally in `__main__`/`main`
  (no-op on Linux). Don't remove. On Windows, `__main__` also runs uvicorn with `loop="none"`.
- Not yet built (deferred per spec): smart routing / provider fallback chains, additional providers
  (Gemini/Groq/etc.), semantic response cache, budget-tracking Kafka consumer + hard budget stops,
  per-agent rate limits, predictive token limiting, the `secretsmanager:` BYOK backend, and the
  cloud/K8s deploy form (canary, PDB, NetworkPolicy, CronJobs).
