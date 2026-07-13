# guardrails-service migrations (Phase 4)

PostgreSQL 16, Atlas convention (same layout as the auth + llms services).

## Files

| File | Purpose |
|------|---------|
| `20260608_0001__init.sql` | Schema `guardrails`, tables, indexes, RLS policies, grants. |
| `20260608_0002__seed.sql` | The 11 first-cycle rule rows + the one platform-default policy. |
| `20260611_0003__policy_authoring.sql` | Policy version-chain plumbing (`root_policy_id`, `stream_mode`, `fail_mode_override`), `policy_audit` table. |
| `20260611_0004__hotpath_redaction_lifecycle.sql` | WP07: `rules.cost_usd` (Contract-19.1 metering); `tenant_redaction_keys` pluggable `key_ref` scheme (`env:`/`sealed:`/legacy `secretsmanager:`) + `version` + resolver/retirement indexes + DELETE grant for the grace-window retirement job. |
| `20260614_0006__pii_coverage.sql` | SSN/IP/address detectors + output PII twins for phone/SSN/IP/address (closes a `/check/output` PII leak). Registry rows + platform-default policy enable. |
| `20260710_0007__passport_mrz.sql` | B3: `pii-passport-mrz-v1` + `output-pii-passport-mrz-v1` (ICAO 9303 MRZ). Registry rows + policy enable. |
| `20260710_0008__canary_leak.sql` | B7: `output-canary-leak-v1` (per-request canary-token leak; inert unless `CANARY_LEAK_ENABLED` + caller `canary_tokens`). Registry row + policy enable. |
| `20260710_0009__pii_context.sql` | B8: `pii-passport-v1` + `pii-name-v1` (context-gated; inert unless `GUARDRAILS_PII_CONTEXT_VALIDATION`). Registry rows + policy enable. |
| `schema.sql` | Flattened end-state snapshot (init + seed + later migrations) — declarative source-of-truth for `atlas schema apply` / drift detection. |
| `atlas.hcl` | Atlas project config (`local` + `ci` envs). |

> **NOTE (on-disk truth):** the table above lists the actual files on disk. Some earlier rows
> were mislabeled historically; trust the filenames. Every new built-in `rule_id` ships a
> registry seed row here AND is enabled in the platform-default policy — otherwise the
> registry-consistency overlay flips `/readyz` to 503.

## Tables & scope (Contract 13)

- **`rules`** (mixed-scope, RLS admits `tenant_id IS NULL`) — rule registry / source of truth for rule IDs. Seeded with the 11 first-cycle platform rules; tenants may add custom rows in Phase 4b.
- **`policies`** (mixed-scope, RLS) — named rule sets. Read platform defaults (`tenant_id IS NULL`) + own; write own. Partial unique indexes enforce exactly one active default per tenant and exactly one active platform default.
- **`agent_policies`** (tenant-scoped, RLS) — per-agent policy assignment.
- **`violations`** (tenant-scoped, RLS, **append-only** — only `SELECT, INSERT` granted) — one row per fired rule. PK is UUID; `request_id` + `trace_id` are `NOT NULL`; `matched_text` stores ONLY the redaction token (PII) or a ≤64-char truncation (non-PII).
- **`tenant_redaction_keys`** (tenant-scoped, RLS) — per-tenant BYO redaction-key references (`secretsmanager:...`).
- **`outbox`** (tenant-scoped, RLS) — transactional outbox; the publisher drains `published_at IS NULL` rows to Kafka. `tenant_id` is backfilled from `partition_key` by a trigger so RLS applies.

## Policy resolution chain (Component 3)

`agent_policies` → tenant default → platform default (`tenant_id IS NULL AND is_default`). The seeded platform default (`policy_id = 00000000-0000-0000-0000-0000000d0001`) enables all 11 rules; its id matches `PLATFORM_DEFAULT_POLICY_ID` in `services/policy_engine.py`.

## Run

Migrations run top-to-bottom on PostgreSQL 16 as a superuser. The runtime role `grd_user` is created idempotently and is **not** a superuser / does **not** bypass RLS.

```bash
# Apply versioned migrations
atlas migrate apply --env local

# Or apply the flattened snapshot
atlas schema apply --env local --to file://schema.sql

# Plain psql (top-to-bottom)
psql "$DATABASE_URL" -f 20260608_0001__init.sql
psql "$DATABASE_URL" -f 20260608_0002__seed.sql
```

The runtime role connects and runs every tenant-scoped query inside
`BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT` (the Core
`in_tenant()` helper).
