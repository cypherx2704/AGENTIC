# cypherx-a1 — database migrations (Contract 14, Atlas)

Versioned PostgreSQL 16 SQL for the `cypherx_a1` schema. Applied by the platform compose
`--profile migrate` job (mounts this dir at `/migrations/cypherx-a1`, runs against the Neon
**DIRECT** endpoint as the migration role `cxa1_ddl`), or locally with Atlas.

| File | What |
|------|------|
| `20260614_0001__init.sql` | schema `cypherx_a1`, runtime role `cxa1_user`, knowledge-graph + ingestion tables, RLS (Contract 13), grants. Idempotent. |
| `20260614_0002__seed.sql` | no-op (per-tenant data is created at runtime). |
| `schema.sql` | flattened end-state snapshot for Atlas drift detection. |
| `atlas.hcl` | Atlas env config (`local`, `ci`). |

## Apply

```bash
# Atlas (preferred)
DATABASE_URL="postgres://cxa1_ddl:...@<DIRECT-neon-host>/cypherx_platform?search_path=cypherx_a1&sslmode=require" \
  atlas migrate apply --env local

# or raw psql (what the compose migrate job does)
psql "$MIGRATE_DATABASE_URL" -f 20260614_0001__init.sql
psql "$MIGRATE_DATABASE_URL" -f 20260614_0002__seed.sql
```

## Rules
- Runtime role `cxa1_user` is **not** a superuser and does **not** bypass RLS; extensions
  (`pgcrypto`) are created by the migration role only (the frozen image's runtime role
  cannot `CREATE EXTENSION`).
- Every tenant-scoped table has `tenant_id NOT NULL`, a tenant-leading index, and an
  `ENABLE + FORCE` RLS policy `USING (tenant_id = NULLIF(current_setting('app.tenant_id', true),'')::uuid)`.
- `outbox` has **RLS disabled** by design (cross-tenant publish queue; isolation in payload).
- Any new tenant-scoped table requires a cross-tenant-denial CI test (see `tests/`).
