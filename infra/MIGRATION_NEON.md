# Migrating CypherX Postgres: Docker → Neon

CypherX uses **one database** (`cypherx_platform`) with **per-service schemas**
(`auth`, `llms`, `guardrails`, `xagent`, `memory`, `rag`, `platform`) and **per-service
login roles** (`auth_user`, `llms_user`, `grd_user`, `xagent_user`, …) enforced by
**Row-Level Security** (Contract 13). All of this maps cleanly onto Neon — with a few
required changes (SSL, pooled-vs-direct endpoints, role passwords).

---

## TL;DR (what actually changes)

| Concern | Local Docker | Neon |
|---|---|---|
| Host | `localhost:5432` (`cypherx-postgres`) | `ep-xxxx[-pooler].<region>.aws.neon.tech` |
| SSL | `sslmode=disable` | **`sslmode=require`** (mandatory) |
| App connections | direct | **pooled endpoint** (`-pooler`, transaction mode) |
| Migrations / DDL | superuser `cypherx_admin` | **direct endpoint** as the Neon **owner** role |
| Superuser | `cypherx_admin` | none (Neon has no superuser; not needed) |
| Role passwords | `localdev` (throwaway) | set real passwords per `*_user` |
| `gen_random_uuid` | `pgcrypto` | built-in PG16 (keep `pgcrypto` anyway) |
| Vector store | `pgvector` image | `CREATE EXTENSION vector` (Neon supports it) |

---

## Why pooled-for-apps / direct-for-migrations

- **Apps → POOLED endpoint** (`...-pooler...`). Neon's pooler is PgBouncer in **transaction
  mode**, which is exactly what the Contract-13 RLS pattern needs: every tenant query runs
  inside a tx that does `SELECT set_config('app.tenant_id', …, true)` (transaction-local).
  Do **not** rely on session-level GUCs on the pooled endpoint — the code already uses
  `SET LOCAL`, so this is fine.
- **Migrations → DIRECT endpoint** (no `-pooler`). Atlas / DDL use **session-level advisory
  locks**, which the transaction pooler does not hold across statements. Always run schema
  changes against the direct endpoint.

---

## Step-by-step

### 1. Create the Neon project + database
- New Neon project (pick a region near your compute). You get a default DB and an **owner**
  role (e.g. `neondb_owner`) that has `CREATEROLE`.
- Create the app database (SQL editor or `psql` to the **direct** endpoint):
  ```sql
  CREATE DATABASE cypherx_platform;
  ```

### 2. Capture both connection strings (Neon dashboard → Connect)
```
# Pooled (apps):
postgresql://<owner>:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/cypherx_platform?sslmode=require
# Direct (migrations):
postgresql://<owner>:<pw>@ep-xxxx.<region>.aws.neon.tech/cypherx_platform?sslmode=require
```

### 3. Enable extensions (direct endpoint, owner)
```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;     -- for memory/rag (enable now even if first-cycle skips it)
```

### 4. Apply the schema (migrations) — DIRECT endpoint, as owner
Two paths:

**(a) Fresh schema (recommended for first-cycle — no data to keep).**
Run each service's `db/migrations/*__init.sql` then `*__seed.sql`. The provided runner does
this in dependency order:
```bash
MIGRATE_DATABASE_URL='postgresql://<owner>:<pw>@ep-xxxx.<region>.aws.neon.tech/cypherx_platform?sslmode=require' \
  bash infra/compose/migrate.sh
# or:  docker compose -f infra/compose/docker-compose.yml --profile migrate up migrate
```
The init scripts `CREATE SCHEMA`, `CREATE ROLE … LOGIN`, tables, RLS policies, and seeds —
all idempotent.

**(b) Move existing data** (only if you have data worth keeping):
```bash
# schema is recreated by the migrations above; dump DATA only:
docker exec cypherx-postgres pg_dump -U cypherx_admin -d cypherx_platform \
  --data-only --no-owner --no-privileges -Fc > cypherx-data.dump
pg_restore --no-owner --no-privileges --data-only \
  -d 'postgresql://<owner>:<pw>@ep-xxxx.<region>.aws.neon.tech/cypherx_platform?sslmode=require' \
  cypherx-data.dump
```

### 5. Set real passwords for the app roles (direct endpoint, owner)
The migrations create the `*_user` roles **without** passwords (locally they used trust/`localdev`).
Neon needs a password to authenticate:
```sql
ALTER ROLE auth_user     WITH LOGIN PASSWORD '<auth_pw>';
ALTER ROLE llms_user     WITH LOGIN PASSWORD '<llms_pw>';
ALTER ROLE grd_user      WITH LOGIN PASSWORD '<grd_pw>';
ALTER ROLE xagent_user   WITH LOGIN PASSWORD '<xagent_pw>';
-- (+ mem_user / rag_user / plat_user when those services land)
```
> **RLS depends on this:** the apps must connect as these **non-owner** `*_user` roles, never
> as the Neon owner. A table's **owner bypasses RLS**, so connecting apps as the owner would
> silently disable tenant isolation. Owner = migrations only; `*_user` = the app.

### 6. Point each service's DSN at the POOLED endpoint
Put these in `infra/compose/.env` (the apps read them; URL-encode the space in `options` as `%20`):
```
# Python services (libpq / psycopg) — note search_path + sslmode=require:
LLMS_POSTGRES_DSN=postgresql://llms_user:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/cypherx_platform?sslmode=require&options=-c%20search_path%3Dllms
GUARDRAILS_POSTGRES_DSN=postgresql://grd_user:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/cypherx_platform?sslmode=require&options=-c%20search_path%3Dguardrails
XAGENT_POSTGRES_DSN=postgresql://xagent_user:<pw>@ep-xxxx-pooler.<region>.aws.neon.tech/cypherx_platform?sslmode=require&options=-c%20search_path%3Dxagent
# Auth (JDBC) — JDBC uses currentSchema, not search_path:
AUTH_DATABASE_URL=jdbc:postgresql://ep-xxxx-pooler.<region>.aws.neon.tech/cypherx_platform?sslmode=require&currentSchema=auth&user=auth_user&password=<pw>
# Migrations (DIRECT endpoint, owner):
MIGRATE_DATABASE_URL=postgresql://<owner>:<pw>@ep-xxxx.<region>.aws.neon.tech/cypherx_platform?sslmode=require
```

### 7. Drop the local Postgres container
The new full-stack compose (`infra/compose/docker-compose.yml`) has **no postgres service** —
the apps point at Neon. (Keep `infra/dev/local/docker-compose.yml` if you still want a fully
offline local DB.)

### 8. Verify
```bash
docker compose -f infra/compose/docker-compose.yml --profile migrate up migrate   # once
docker compose -f infra/compose/docker-compose.yml up -d --build
# then: curl each /readyz (expect 200), run the finale smoke
```

---

## Gotchas checklist
- [ ] `sslmode=require` on **every** DSN (psycopg **and** JDBC) — Neon refuses plaintext.
- [ ] Apps on the **pooled** endpoint; migrations on the **direct** endpoint.
- [ ] Apps connect as `*_user` (non-owner) so **RLS stays enforced**.
- [ ] `CREATE EXTENSION pgcrypto, vector` once on the direct endpoint.
- [ ] `search_path` via `options=-c search_path=<schema>` (libpq) / `currentSchema=` (JDBC).
- [ ] First request after idle is slow (Neon **auto-suspend** cold start) — the services'
      fail-soft pool-open tolerates it; `/readyz` may briefly 503 while the compute wakes.
      For demos, raise the autosuspend timeout or use a paid always-on compute.
- [ ] Neon connection limits are lower than a local PG — the pooled endpoint + the services'
      modest pool sizes are fine; don't crank `max_size`.
- [ ] The outbox publisher drains cross-tenant with **no** `app.tenant_id` set — that's why
      the `outbox` tables have RLS **disabled**; nothing Neon-specific, just don't "fix" it.

---

## WP14 — full-stack apply order (all services)

The full-stack compose (`infra/compose/docker-compose.yml`) now brings up the **whole platform**: the original four
(auth / llms / guardrails / xagent) **plus** rag / memory / tool-registry (WP09–11) and the
frontend BFF + SPA (WP13). The `migrate` job applies every service's migrations **and** provisions the new runtime
roles. Run it ONCE, against the **DIRECT** endpoint, **before** the app services.

### What `--profile migrate` does (exact order)

`infra/compose/migrate.sh` runs, against `MIGRATE_DATABASE_URL` (DIRECT endpoint, owner role):

0. `CREATE EXTENSION pgcrypto, vector` (idempotent). **`vector` is now REQUIRED** — rag + memory ship `*_vectors_1536`
   tables and pgvector HNSW indexes. If `CREATE EXTENSION vector` fails, rag/memory migrations will fail; enable it in
   the Neon console first (`CREATE EXTENSION vector;`).
1. Per-service `*__init.sql` then `*__seed.sql`, **in this dependency order**:
   `auth → llms → guardrails → xagent → rag → memory → tool-registry`.
   - auth first (tenants + role backbone). Each `*__init.sql` is idempotent (`CREATE … IF NOT EXISTS`, DO-block role
     guards), so re-running is safe.
   - **Already-existing services' new WP05–WP12 migrations** (e.g. auth `…__webhooks.sql`, the guardrails/llms WP05–07
     additions, xagent WP08/WP12) are just more timestamp-ordered files in those same `db/migrations/` dirs — the
     runner picks them up automatically in lexical order. No manual step.
2. **Runtime-role provisioning** (idempotent `ALTER ROLE`): for each `*_user` role that exists, set
   `search_path = <schema>, public` (always) and `WITH LOGIN PASSWORD '<pw>'` (only if the matching `*_DB_PASSWORD`
   env var is non-empty). Roles + schemas:

   | role          | schema       | password env var          | DSN var (POOLED, app) |
   |---------------|--------------|---------------------------|------------------------|
   | `auth_user`   | `auth`       | `AUTH_DB_PASSWORD`        | `AUTH_DATABASE_URL` (JDBC; pw is separate `AUTH_DB_PASSWORD`) |
   | `llms_user`   | `llms`       | `LLMS_DB_PASSWORD`       | `LLMS_DATABASE_URL` |
   | `grd_user`    | `guardrails` | `GUARDRAILS_DB_PASSWORD` | `GUARDRAILS_DATABASE_URL` |
   | `xagent_user` | `xagent`     | `XAGENT_DB_PASSWORD`    | `XAGENT_DATABASE_URL` |
   | `rag_user`    | `rag`        | `RAG_DB_PASSWORD`       | `RAG_DATABASE_URL` |
   | `mem_user`    | `memory`     | `MEM_DB_PASSWORD`       | `MEMORY_DATABASE_URL` |
   | `tool_user`   | `tools`      | `TOOL_DB_PASSWORD`      | `TOOL_REGISTRY_DATABASE_URL` |

   > Each `*_DB_PASSWORD` MUST equal the password embedded in that service's `*_DATABASE_URL` (POOLED). The apps connect
   > as these **non-owner** roles so **RLS stays enforced** (the owner bypasses RLS — never run the apps as the owner).

### First-run commands

```bash
cd infra/compose
cp .env.example .env            # fill the 7 *_DATABASE_URL (POOLED) + MIGRATE_DATABASE_URL (DIRECT)
                                # + the 7 *_DB_PASSWORD + SESSION_KEK_BASE64
docker compose --profile migrate up migrate     # 1) schema + roles + role passwords/search_path (once)
docker compose up -d --build                    # 2) deps + topics-init + the full platform + the edge proxy
docker compose --profile observability up -d    # 3) optional: collector + Tempo + Loki + Prometheus + Grafana
```

### Verify

```bash
# every service /readyz (host port-forwards):
for p in 8080 8085 8086 8083 8087 8088 8089; do curl -fsS localhost:$p/readyz && echo " <- $p ok"; done
curl -fsS localhost:8092/livez   # BFF
curl -fsS localhost:8000/healthz # edge entrypoint
# topics created:
docker exec cypherx-redpanda rpk topic list -X brokers=localhost:9092
```
