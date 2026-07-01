# `modules/postgres-bootstrap` — Database Initialisation (Component 16)

Terraform-owned, **runs once per environment**, idempotent. Provisioned via
`environments/<env>/postgres-bootstrap/terragrunt.hcl`.

## What this module owns (and what it does NOT)

| Owner | Owns |
|-------|------|
| **This module** (`cyrilgdn/postgresql` provider) | `CREATE DATABASE cypherx_platform`; `CREATE EXTENSION vector` + `pg_stat_statements`; the **7 schemas** (`auth`, `llms`, `guardrails`, `memory`, `rag`, `xagent`, `platform`); per-service **runtime users** (`*_user`, least-priv + `ALTER DEFAULT PRIVILEGES`); per-service **DDL users** (`*_ddl`, `CREATE,USAGE` + `CREATEROLE`); the schema/default-privilege grants. |
| **Atlas** (per service, K8s Job, every deploy) | Tables, columns, indexes, **RLS policies**, RLS roles — WITHIN the service's own schema, using its `*_ddl` user. |

No service migration touches another service's schema (CI-enforced — Contract 14). This split resolves the
Contract 14 ownership ambiguity.

## Users created

| Service key | Schema | Runtime user (`*_user`) | DDL user (`*_ddl`) |
|-------------|--------|-------------------------|--------------------|
| auth | `auth` | `auth_user` | `auth_ddl` |
| llms | `llms` | `llms_user` | `llms_ddl` |
| guardrails | `guardrails` | `grd_user` | `grd_ddl` |
| memory | `memory` | `mem_user` | `mem_ddl` |
| rag | `rag` | `rag_user` | `rag_ddl` |
| xagent | `xagent` | `xagent_user` | `xagent_ddl` |
| platform | `platform` | `plat_user` | `plat_ddl` |

(`*_user` / schema mapping matches the PgBouncer databases in Component 14.)

### Privilege model

- **Runtime `*_user`** — `LOGIN`, `USAGE` on its own schema, and via `ALTER DEFAULT PRIVILEGES`
  `SELECT,INSERT,UPDATE,DELETE` on future tables + `USAGE,SELECT` on future sequences. **No** `CREATE`, **no**
  `CREATEROLE`, **no** `CREATEDB`, **not** superuser. This is the credential the service pod uses at runtime.
- **DDL `*_ddl`** — `LOGIN`, owns the schema, `CREATE,USAGE` on its own schema, plus **`CREATEROLE`** so Atlas
  can create the per-schema RLS role. This credential is mounted ONLY into that service's Atlas migration Job.

## ⚠️ Known limitation — `CREATEROLE` is cluster-wide, not schema-scoped

Postgres does **not** support scoping `CREATEROLE` to a single schema. Each `*_ddl` user can therefore technically
create or alter any non-superuser role in the cluster (it cannot touch superusers, or roles created by superusers
it does not own). Contract 14's "grant `CREATEROLE` on the service's schema only" is **aspirational** — Postgres
lacks the primitive. Mitigations (do not remove):

1. The `*_ddl` password lives only in Doppler at `db/<service>/ddl_password` and is mounted **only** into that
   service's Atlas migration Job — not the runtime pod, not other services.
2. The Helm chart that runs the Job uses a dedicated ServiceAccount with no other secret access.
3. Audit-log review (CloudTrail + RDS `pg_audit`, Phase 13) flags any `CREATE ROLE` issued by a `*_ddl` user
   against an unexpected role name.

**Do NOT** grant `CREATEROLE` to runtime users. **Do NOT** share a single DDL user across services.

## Secrets (no plaintext, ever)

- **Bootstrap (superuser) connection** — connects as the RDS master. The password is resolved at apply time from
  the AWS-managed RDS master secret (`master_user_secret_arn`, because the `postgresql` stack sets
  `manage_master_user_password = true`) or from `TF_VAR_pg_superuser_password` (Doppler). It is never written to
  this repo and is `sensitive` in state.
- **Runtime passwords** — `var.runtime_passwords[<service-key>]` ← Doppler `db/<service>/runtime_password`,
  injected as `TF_VAR_runtime_passwords` (a map). No default → a missing value fails the apply loudly.
- **DDL passwords** — `var.ddl_passwords[<service-key>]` ← Doppler `db/<service>/ddl_password`, injected as
  `TF_VAR_ddl_passwords`. No default.

Example (operator shell or CI), passwords pulled from Doppler then handed to Terraform as JSON maps:

```bash
export TF_VAR_runtime_passwords="$(doppler secrets get --plain DB_RUNTIME_PASSWORDS_JSON --config dev)"
export TF_VAR_ddl_passwords="$(doppler secrets get --plain DB_DDL_PASSWORDS_JSON --config dev)"
terragrunt apply --terragrunt-working-dir environments/dev/postgres-bootstrap
```

## Inputs (key)

| Variable | Description |
|----------|-------------|
| `pg_host`, `pg_port` | RDS endpoint (from the `postgresql` stack via `dependency`). |
| `pg_superuser` | RDS master username. |
| `master_user_secret_arn` | AWS-managed master secret ARN; password read from it when `pg_superuser_password` is empty. |
| `database_name` | Default `cypherx_platform`. |
| `runtime_passwords` / `ddl_passwords` | `map(string)` keyed by service key (`auth`, `llms`, …). **Sensitive, required.** |

## Outputs

`database_name`, `schemas`, `runtime_users`, `ddl_users`, `extensions`. **No passwords are output.**

## Idempotency

`postgresql_database`, `postgresql_extension` (with the provider's `IF NOT EXISTS` semantics), `postgresql_schema`,
`postgresql_role`, `postgresql_grant`, and `postgresql_default_privileges` are all declarative — re-applying is a
no-op once converged. Re-running after a password rotation in Doppler updates the role password in place.
