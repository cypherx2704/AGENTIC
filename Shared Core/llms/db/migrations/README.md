# llms-gateway migrations (Phase 3)

PostgreSQL 16, Atlas convention (same layout as the auth service).

## Files

| File | Purpose |
|------|---------|
| `20260608_0001__init.sql` | Schema `llms`, tables, indexes, RLS policies, grants. |
| `20260608_0002__seed.sql` | `provider_pricing` rates + platform `model_aliases`. |
| `20260610_0003__llm_call_id_and_capabilities.sql` | WP02: `usage_records.llm_call_id` billing key (replaces unique `(tenant_id, request_id)`), `model_capabilities` table + seed, `code`/`vision` alias seed reconciliation. |
| `20260610_0004__llms_wp05_rate_limits.sql` | WP05: `rate_limits` per-plan-tier reference config (free/pro/enterprise) + seed mirroring the Auth `plan_defaults` `llms` block. |
| `20260610_0005__llms_embeddings.sql` | WP06: `usage_records.operation` column (nullable, default `'chat'`), `embed` platform alias → `openai/text-embedding-3-small`, and `provider_pricing` + `model_capabilities` (`embedding_dim=1536`) seeds for `text-embedding-3-small`. Powers `POST /v1/embeddings`. |
| `20260612_0008__llms_rerank_classify.sql` | Additive: `rerank-default` alias → `cypherx/rerank-mock-v1` and `safety-default` alias → `cypherx/classify-stub-v1`, plus `provider_pricing` (all rates 0 — metered by UNITS) and `model_capabilities` seeds for both. Powers `POST /v1/rerank` (`operation='rerank'`) and `POST /v1/classify` (`operation='classify'`). No schema change — `usage_records.operation` is unconstrained `VARCHAR(20)`. |
| `schema.sql` | Flattened end-state snapshot (all migrations) — declarative source-of-truth for `atlas schema apply` / drift detection. |
| `atlas.hcl` | Atlas project config (`local` + `ci` envs). |

## Tables

- **`usage_records`** (tenant-scoped, RLS) — one row per LLM call: token counts, cost, gateway-minted `llm_call_id` (unique `(tenant_id, llm_call_id)` — THE billing key), `request_id` (non-unique correlation; one upstream `X-Request-ID` may span multiple calls), non-null `trace_id`, `api_key_id`, `principal_type`, `duration_ms`, `operation` (`'chat'` default | `'embedding'` | `'rerank'` | `'classify'`; unconstrained `VARCHAR(20)`).
- **`outbox`** (tenant-scoped, RLS) — transactional outbox; the publisher drains `published_at IS NULL` rows to Kafka. `tenant_id` is backfilled from `partition_key` by a trigger so RLS applies.
- **`provider_pricing`** (platform-scoped, no RLS) — per-1k-token rates; PR-managed, read-only at runtime.
- **`model_aliases`** (mixed-scope, RLS admits `tenant_id IS NULL`) — platform defaults + per-tenant overrides.
- **`model_capabilities`** (platform-scoped, no RLS) — per-model caps (`max_tokens_cap`, `context_window`, `supports_*`, `embedding_dim`); DB-authoritative, seeded, PR-managed.
- **`rate_limits`** (platform-scoped, no RLS) — per-plan-tier (`free`/`pro`/`enterprise`) rate + token + cost caps mirroring the Contract-19 `llms` block; the fallback source for the WP05 plan→limits resolver (`services/auth_client.py`), read-only at runtime.

## Run

Migrations run top-to-bottom on PostgreSQL 16 as a superuser. The runtime role `llms_user` is created idempotently (exists in dev/local) and is **not** a superuser / does **not** bypass RLS.

```bash
# Apply versioned migrations
atlas migrate apply --env local

# Or apply the flattened snapshot
atlas schema apply --env local --to file://schema.sql

# Plain psql (top-to-bottom)
psql "$DATABASE_URL" -f 20260608_0001__init.sql
psql "$DATABASE_URL" -f 20260608_0002__seed.sql
psql "$DATABASE_URL" -f 20260610_0003__llm_call_id_and_capabilities.sql
```

The runtime role connects and runs every tenant-scoped query inside
`BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT` (the Core
`in_tenant()` helper).
