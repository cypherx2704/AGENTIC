# CypherX SharedCore — LLMs Gateway

Unified, provider-normalized LLM gateway (Phase 3). Every service routes LLM traffic
through this gateway, which translates a single OpenAI-superset request format to/from
Anthropic and OpenAI, meters token usage + cost, and emits billing events via a
transactional outbox.

This repo currently implements the **critical-path spine**: `POST /v1/chat/completions`
(streaming + non-streaming) and `POST /v1/embeddings` (WP06), dual-mode JWT auth, the
full unified request/response schema (tools / tool_calls / image_url already modelled),
the provider normalizer, cost calculation, the usage-record + outbox write path with a
Kafka publisher, the `/models` / `/usage` / `/cost` read APIs, WP05 rate-limiting +
idempotency, and the health/metrics endpoints. Deferred (open from day one but not yet
built): BYOK and the SSRF-hardened multimodal fetcher.

### Embeddings (`POST /v1/embeddings`)

OpenAI-shaped embeddings for the RAG + Memory services. Resolves the `embed` platform
alias → `openai/text-embedding-3-small`; `dimensions` is honored (Matryoshka
truncation). Caps (config-keyed, env-overridable): `EMBEDDINGS_MAX_INPUT_ITEMS` (256)
and `EMBEDDINGS_MAX_PAYLOAD_BYTES` (25 MiB) → `413 VALIDATION_ERROR` over a cap.
`Idempotency-Key` gets full Contract-9 support (replay / `409` / fail-open). Cost is
billed on input tokens only (output 0 by convention) and recorded with
`operation="embedding"`. Under `MOCK_PROVIDERS=true` the mock provider returns
deterministic unit-norm pseudo-vectors of the requested dimension with no network.

```bash
curl -s localhost:8000/v1/embeddings \
  -H 'Authorization: Bearer <agent-jwt>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"embed","input":["hello","world"],"dimensions":1536}'
```

## Run locally

```bash
# Install deps (already done if `.venv` exists)
python -m uv sync

# Run with the deterministic mock provider — NO provider keys, NO DB, NO Kafka required.
MOCK_PROVIDERS=true python -m uv run uvicorn llms_gateway.main:app --reload

# Smoke test
curl -s localhost:8000/livez
```

A request (auth is required in real mode; see below):

```bash
curl -s localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer <agent-jwt>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"smart","messages":[{"role":"user","content":"Hello"}]}'
```

### Configuration

All settings come from the environment (see `.env.example`). Key ones:

| Env var | Purpose | Local default |
|---------|---------|---------------|
| `MOCK_PROVIDERS` | Use the deterministic mock provider (no keys/network). | `false` |
| `DATABASE_URL` | psycopg DSN (`postgresql://llms_user:...@host:5432/cypherx_platform`). | localhost |
| `KAFKA_BROKERS` | aiokafka bootstrap servers. | `localhost:9092` |
| `AUTH_JWKS_URL` | JWKS document of the running auth-service. | localhost |
| `AUTH_ISSUER_URL` | Expected JWT `iss`. | localhost |
| `AUTH_PLATFORM_AUDIENCE` | Required JWT `aud` entry. | `cypherx-platform` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Platform provider keys (real mode). | unset |

Point `AUTH_JWKS_URL` at the running auth-service
(`http://auth-service.shared-core.svc.cluster.local:8080/.well-known/jwks.json`
in-cluster, or your local auth-service URL).

### Auth modes (Contract 12)

- **External:** `Authorization: Bearer <agent-jwt>` only.
- **Internal:** `Authorization: Bearer <service-jwt>` + `X-Forwarded-Agent-JWT: <agent-jwt>`;
  the service token's `on_behalf_of` must equal the forwarded agent's `agent_id`.

Both require scope `llm:invoke`. `tenant_id` / `agent_id` come only from the JWT (Contract 13).

## Tests

```bash
python -m uv run pytest
```

Tests run with **no** Auth, DB, or Kafka: the normalizer tests are pure functions, and
the chat test uses the mock provider with the auth dependency overridden and the DB pool
absent (usage-write no-ops).

## Lint / type-check

```bash
python -m uv run ruff check src tests
python -m uv run mypy
```

## Database

Migrations live in `src/llms_gateway/db/migrations/` (Atlas convention). See the
[migrations README](src/llms_gateway/db/migrations/README.md). Tables: `usage_records`,
`outbox` (tenant-scoped, RLS), `provider_pricing` (platform-scoped), `model_aliases`
(mixed-scope).
