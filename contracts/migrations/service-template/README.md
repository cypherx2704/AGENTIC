# Service migration template (Contract 14)

A minimal, conforming reference layout for a service's PostgreSQL migrations. Copy it into
`<service-repo>/db/` and replace every occurrence of `example` with your service name.

See the full standard: [`../atlas-conventions.md`](../atlas-conventions.md).

## Contents

| File | Purpose |
|------|---------|
| `atlas.hcl` | Atlas project config: declarative `schema.sql` source, `migrations/` dir, dev DB for diffing, destructive-change lint policy. |
| `schema.sql` | Declarative snapshot of the desired state: one tenant-scoped table (`example.widgets`) with `tenant_id`, a `tenant_id`-leading index, RLS policy, and the least-privilege runtime role. |
| `migrations/0001__init.sql` | The initial versioned migration that brings an empty DB up to `schema.sql`. Expand-only (create-only). |

## Wiring into a service repo

1. Copy this directory to `<service-repo>/db/` and rename `example` → `<service>` everywhere
   (schema, table, role `<service>_runtime`, Atlas `env`).
2. **Doppler credentials** (Contract 14 §4) — mandatory paths the Helm chart resolves:
   - DDL (migration Job): `db/<service>/ddl_password`
   - Runtime (service): `db/<service>/runtime_password`
3. **Helm hook** (Contract 14 §4): run migrations as a K8s `Job` with
   `"helm.sh/hook": pre-install,pre-upgrade`, using the **DDL** user. The Job must complete before
   the service Deployment becomes `Ready`.
4. **Grant the migration role `CREATEROLE` on this schema only** (Contract 14 §8) so it can create
   `<service>_runtime`.

## Day-to-day commands

```bash
# Lint the migration directory (destructive-change gate — Contract 14 §3).
atlas migrate lint --env example --latest 1

# Diff the declarative snapshot against the migration history (drift gate — Contract 14 §3).
atlas schema diff --env example

# Create a new versioned migration after editing schema.sql (timestamped name, Contract 14 §2).
atlas migrate diff <name> --env example

# Apply pending migrations (CI integration test runs this against a real Postgres container).
atlas migrate apply --env example --url "$DATABASE_URL"
```

## Rules this template demonstrates

- **Contract 13 §4:** `tenant_id UUID NOT NULL`, a `tenant_id`-leading index, RLS `ENABLE`/`FORCE`
  with `USING (tenant_id = current_setting('app.tenant_id')::uuid)`. A new tenant-scoped table also
  requires a **cross-tenant denial** CI test before merge.
- **Contract 14 §4:** least-privilege per-service runtime role distinct from the DDL user.
- **Contract 14 §5:** the init migration is **expand-only** (create-only, no destructive DDL).
- **Contract 14 §8:** RLS + runtime role are part of this service's own schema; the migration only
  touches the `example` schema.
