# auth-service — database migrations

PostgreSQL 16. Versioned, **runnable top-to-bottom as a superuser** (or the migration role).

## Files

| File | Purpose |
|------|---------|
| `20260606_0001__init.sql` | Schema, every first-cycle table, indexes, Row Level Security (Contract 13), grants to the runtime role `auth_user`. |
| `20260606_0002__seed.sql` | Well-known tenants, `plan_defaults` (free/pro/enterprise), the `default-allow-first-cycle` platform policy, and the 5 first-cycle `service_acl` edges. Idempotent (`ON CONFLICT DO NOTHING`). |
| `20260610_0003__outbox.sql` | Transactional outbox `auth.outbox` (Phase 2 Amendment Log 2026-06 / WP02): durable `cypherx.tenant.*`, `token.revoked`, `policy.changed`, `config.updated` events committed in the same transaction as their state change; drained by the in-service `OutboxRelay`. Idempotent. |
| `20260611_0006__onboarding.sql` | WP04 Component 1c (self-serve onboarding): extends `auth.signup_attempts` additively — `verification_token_hash`, `tenant_name`, `attempts`; relaxes `full_name`/`terms_version_accepted`/`verification_token` to NULLABLE; adds the token-hash unique index + status/created index. Idempotent. |
| `20260614_0009__service_acl_seed.sql` | Additive `service_acl` caller edges keyed by the SHORT `X-Service-Name` principals (`xagent`, `llms`, `guardrails`, `rag`, `memory`) so each service can mint a service token + call `/v1/authorize`. Fixes the first-cycle seed gap (0002 only had long target-style caller names) and the rag/memory cross-repo seeds that used the wrong columns. Idempotent (`ON CONFLICT DO NOTHING`); modifies no existing row. |
| `schema.sql` | Flattened snapshot of the desired end-state (init + seed + outbox). Declarative source-of-truth for Atlas drift detection. Regenerate after schema changes. |
| `atlas.hcl` | [Atlas](https://atlasgo.io) project config (`local` / `ci` envs). |

## Apply (raw psql, no tooling)

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 20260606_0001__init.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 20260606_0002__seed.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f 20260610_0003__outbox.sql
```

`$DATABASE_URL` must point at a superuser (the init script creates the `auth_user` role and
the `pgcrypto` / `citext` extensions). Both files are idempotent — safe to re-run.

## Apply (Atlas)

```bash
atlas migrate apply --env local                       # apply pending versioned files
atlas schema apply  --env local --to file://schema.sql # converge to the snapshot
atlas migrate diff  new_change --env local             # author a new versioned file from schema.sql
```

## Table scope (Contract 13)

**Tenant-scoped** (have `tenant_id`, a tenant-leading index, RLS `USING app.tenant_id`):
`agents`, `api_keys`, `audit_log`, `policies`*, `service_clients`, `tenant_quotas`,
`behavior_policies`, `approval_requests`, `approval_grants`.

> *`policies` and `behavior_policies` hold both per-tenant rows and platform-default rows
> (`tenant_id IS NULL`). Their RLS policy admits `tenant_id IS NULL` so every tenant
> transaction can read the platform default.

**Platform-scoped** (no RLS — mutated only by Auth itself):
`tenants`, `signing_keys`, `service_acl`, `bootstrap_state`, `plan_defaults`,
`upstream_identity`, `upstream_service_issuers`, `revoked_tokens`, `signup_attempts`,
`outbox` (the relay drains every tenant's rows in one pass).

## RLS contract

The runtime role `auth_user` is **not** a superuser and does **not** have `BYPASSRLS`, so RLS
is enforced. Every tenant-scoped access goes through the Core `TenantTx` helper, which runs:

```sql
BEGIN;
SET LOCAL app.tenant_id = '<uuid>';
-- ... queries ...
COMMIT;
```

Platform-scoped access uses `TenantTx.inPlatform { ... }` (a plain transaction with **no**
`app.tenant_id` set — touching a tenant-scoped table there returns zero rows by design).

## Audit append-only

`auth.audit_log` grants `auth_user` only `SELECT, INSERT` — **UPDATE and DELETE are not
granted** (tamper-evidence; Component 6). The per-tenant hash chain (`row_hash` /
`prev_row_hash`) lets auditors detect any rewrite. A separate retention-purge role (not
created in first cycle) holds `DELETE` for the nightly 90-day purge.

## SYSTEM-USER sentinel

`auth.agents.created_by` is `NOT NULL`. Bootstrap / manual-seed agents with no px0 user behind
them use the reserved constant `00000000-0000-0000-0000-000000000000`
(`ai.cypherx.auth.domain.SYSTEM_USER_ID`). It is not a row in any table — px0 owns users.
