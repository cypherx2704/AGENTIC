# Repo placement, bootstrap & deployment

> How cypherx-a1 (Autonomous Engineering Memory) and its `mcp-eng-memory` facade land in the CypherX workspace, get their `cypherx_a1` schema + `cxa1_user` role bootstrapped (compose `migrate` job + `migrate.sh` + the two cross-team edits), wire into `infra/compose/docker-compose.yml` (ports `8093`/`8094`), seed their `auth.service_acl` edges with the canonical columns, register `mcp-eng-memory@1.0.0` in the tool-registry, and run fully keyless locally.

This document is the operational contract for getting cypherx-a1 onto a developer machine and (later) into a cluster. It is precise about file paths, table/column names, role names, env-var names, and ports — quote it, do not paraphrase it. Everything here is grounded in the real source: `infra/compose/docker-compose.yml`, `infra/compose/migrate.sh`, `CoreProjects/cypherx-a1/db/migrations/20260614_0001__init.sql`, `..._0002__seed.sql`, `infra/dev/local/seed/postgres-init.sql`, `infra/modules/postgres-bootstrap/main.tf`, `mcp-eng-memory/manifest.json`, and `src/cypherx_a1/core/config.py`.

---

## 1. Where the code lives (CoreProjects placement)

cypherx-a1 is a **consuming application** — a first-class peer of `xAgent/ax-1`, **not** a SharedCore service. SharedCore services (`auth`, `llms`, `guardrails`, `rag`, `memory`) are the reusable, separately-billable SaaS primitives. cypherx-a1 *consumes* them through their versioned `/v1` contracts and pushes **no business logic into SharedCore**. To reflect that boundary in the directory tree, it lives under `CoreProjects/`, not under `Shared Core/`.

| Path | Holds |
| --- | --- |
| `CoreProjects/cypherx-a1/` | The product service repo (FastAPI, package `cypherx_a1`, owns the `cypherx_a1` Postgres schema). |
| `CoreProjects/cypherx-a1/src/cypherx_a1/` | All domain logic: connectors, ingestion, extraction, retrieval, copilot, graph repo, SharedCore clients, outbox. |
| `CoreProjects/cypherx-a1/db/migrations/` | `20260614_0001__init.sql` (schema + role + RLS), `20260614_0002__seed.sql` (`auth.service_acl` edges), `schema.sql`, `atlas.hcl`. |
| `CoreProjects/cypherx-a1/mcp-eng-memory/` | The **separate, stateless** MCP server (own package `mcp_eng_memory`, own `Dockerfile`, `manifest.json`, `pyproject.toml`). No DB / Kafka / outbox. |
| `CoreProjects/cypherx-a1/docs/` | Product development docs (this file is `docs/13-...`). |

**Why this placement matters for build contexts:** `infra/compose/docker-compose.yml` references the repo by a path that is **two levels up** from `infra/compose/` (build contexts are written relative to the compose file). So:

- `cypherx-a1` build context = `../../CoreProjects/cypherx-a1`
- `mcp-eng-memory` build context = `../../CoreProjects/cypherx-a1/mcp-eng-memory`

If the repo were placed under `Shared Core/`, those contexts (and the migrate mount, and the bootstrap enumerations) would all be wrong. The `CoreProjects/` placement is load-bearing.

> Boundary reminder: the **graph is the crown jewel and is app-owned**. It lives only in the `cypherx_a1` schema. It never enters RAG (RAG holds opaque text chunks + a `vector_ref`) and never enters Memory (per-principal episodic only). That ownership split is what keeps cypherx-a1 a consumer, not a fork of SharedCore.

---

## 2. The schema/role bootstrap — four moving parts

cypherx-a1 needs four things provisioned before its container can serve traffic:

1. the `cypherx_a1` **schema** (the graph + ingestion tables + RLS),
2. the runtime **role** `cxa1_user` (LOGIN, non-superuser, no BYPASSRLS, cannot `CREATE EXTENSION`),
3. that role's **password + `search_path`** (so the app can authenticate on the Neon **POOLED** endpoint),
4. the `auth.service_acl` **edges** that let cypherx-a1 mint Contract-12 service tokens for the SharedCore services it calls.

These land via **four files**, depending on which environment you are in:

| # | Concern | Local compose (Neon) | Local deps-only (Tilt/kind pg container) | Cloud (AWS RDS) |
| --- | --- | --- | --- | --- |
| 1 | schema + tables + RLS + `cxa1_user` (passwordless) | `db/migrations/20260614_0001__init.sql` (via `migrate` job) | `infra/dev/local/seed/postgres-init.sql` | Atlas applies `…__init.sql`; `postgres-bootstrap` makes the role |
| 2 | role password + `search_path` | `infra/compose/migrate.sh` step 8 (`set_role_pw cxa1_user`) | baked into `postgres-init.sql` (`CYPHERX_LOCAL_DB_PASSWORD`) | `modules/postgres-bootstrap/main.tf` (Doppler password) |
| 3 | `auth.service_acl` edges | `db/migrations/20260614_0002__seed.sql` (via `migrate` job, after auth) | applied with the same seed if mounted | seeded by the same migration / auth admin |
| 4 | cross-team role enumeration | — | `postgres-init.sql` closed list | `postgres-bootstrap/main.tf` closed list (needs Doppler `db/cypherx-a1/{runtime,ddl}_password`) |

The rest of this section walks each part.

### 2.1 `…__init.sql` — schema, role, tables, RLS

`db/migrations/20260614_0001__init.sql` runs as the migration/owner role (`cxa1_ddl` in cloud; the Neon owner role locally). It is **idempotent** (`CREATE … IF NOT EXISTS`, `DO $$ … $$` role guards) and does, in order:

1. `CREATE EXTENSION IF NOT EXISTS pgcrypto;` — for `gen_random_uuid()`. (Extensions are created by the migration role only; the runtime role cannot.)
2. `CREATE SCHEMA IF NOT EXISTS cypherx_a1;`
3. Creates the runtime role **without a password**:
   ```sql
   IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cxa1_user') THEN
     CREATE ROLE cxa1_user LOGIN;
   END IF;
   ```
4. `GRANT USAGE ON SCHEMA cypherx_a1 TO cxa1_user;`
5. Creates the tenant-scoped tables and the one platform-internal table.
6. Enables **and FORCEs** RLS on every tenant-scoped table with a `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` policy.
7. `GRANT`s the runtime role exactly the DML it needs (RLS still applies on top).

**Tables created** (schema `cypherx_a1`):

| Table | RLS? | Role grants | Purpose |
| --- | --- | --- | --- |
| `entities` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | Graph nodes (bitemporal; `fts` generated `tsvector`; `vector_ref` JSONB; partial unique `uq_entities_natural_current` on the current slice). |
| `edges` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | Typed bitemporal relationships (adjacency list; indexes `idx_edges_src` `(tenant,src,rel)`, `idx_edges_dst` `(tenant,dst,rel)`, partial `idx_edges_current` WHERE `valid_to IS NULL`). |
| `identities` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | Cross-tool alias → canonical person entity. |
| `raw_events` | yes (FORCE) | SELECT/INSERT | Immutable landing (idempotent on `(tenant_id, source, external_id, content_sha)`). |
| `connectors` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | Per-tenant connector installs + non-secret config. |
| `connector_secrets` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | KMS/BYOK-sealed credentials (`sealed:v1:…` / `env:<NAME>`). |
| `sync_cursors` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | Resumable per-`(tenant,connector,stream)` sync position. |
| `extraction_jobs` | yes (FORCE) | SELECT/INSERT/UPDATE | Idempotency + cost ledger for LLM extraction (PK `(tenant_id, node_id, content_sha, extractor_version)`; `llm_call_id` is the billing key). |
| `citations` | yes (FORCE) | SELECT/INSERT/DELETE | RAG chunk/doc → graph entity/edge provenance. |
| `resource_acls` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | **App-owned** per-repo/per-team read rules (the tenancy decision). |
| `rag_kbs` | yes (FORCE) | SELECT/INSERT/UPDATE/DELETE | Resolved RAG KB bindings with the **pinned** embedding model + dim (immutable). |
| `outbox` | **NO RLS** | SELECT/INSERT/UPDATE | Cross-tenant publish queue (Contract-5 envelopes; `partition_key = tenant_id`). |

The RLS loop is the canonical Contract-13 pattern:

```sql
ALTER TABLE cypherx_a1.<t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE cypherx_a1.<t> FORCE  ROW LEVEL SECURITY;
CREATE POLICY <t>_isolation ON cypherx_a1.<t> FOR ALL
  USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
```

`outbox` is explicitly `DISABLE ROW LEVEL SECURITY` — it is drained by a background publisher that sets no `app.tenant_id`, so a tenant policy would block the drain. Isolation for the outbox lives in the **payload** (the Contract-5 envelope's `tenant_id`/`partition_key`), not the row.

> **Why `cxa1_user` is passwordless here:** the init script creates the role `LOGIN` but with no password and no default `search_path`. Neon needs a password to authenticate, and the app connects on the **POOLED** endpoint as this non-owner role so RLS stays enforced. The password + `search_path` are applied separately by `migrate.sh` (step 8 below).

### 2.2 `migrate.sh` — the compose `migrate` job

`infra/compose/migrate.sh` is the one-shot bootstrap mounted into the `migrate` service. It runs against the Neon **DIRECT** endpoint (`MIGRATE_DATABASE_URL`, owner role) because the init scripts take session-level advisory locks / DDL the POOLED (transaction-mode) endpoint cannot hold across statements.

Its loop iterates the service list **in dependency order** and applies each service's `*__init.sql` then `*__seed.sql`:

```sh
for svc in auth llms guardrails xagent rag memory tool-registry cypherx-a1; do
  dir="/migrations/$svc"
  ...
  for f in $(ls "$dir"/*__init.sql 2>/dev/null | sort); do run_file "$f"; done
  for f in $(ls "$dir"/*__seed.sql 2>/dev/null | sort); do run_file "$f"; done
done
```

**cypherx-a1 is last in the list — deliberately.** Its `…_0002__seed.sql` inserts into `auth.service_acl`, which only exists after `auth` has been migrated. Running cypherx-a1 after `auth` guarantees the table is there.

After all services migrate, **step 8** provisions the runtime-role passwords + `search_path` from env via the `set_role_pw` helper. The cypherx-a1 line is:

```sh
set_role_pw cxa1_user   cypherx_a1 "${CYPHERXA1_DB_PASSWORD:-}"
```

`set_role_pw <role> <schema> <password>` does two things, both guarded so a not-yet-migrated role is skipped rather than failing the run:

1. **search_path — always** (idempotent; no secret):
   ```sql
   DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cxa1_user')
     THEN EXECUTE 'ALTER ROLE cxa1_user SET search_path = cypherx_a1, public'; END IF; END $$;
   ```
2. **password — only when `CYPHERXA1_DB_PASSWORD` is non-empty.** The password is passed as a psql client variable and emitted via `:'pw'` (correctly single-quoted/escaped):
   ```sql
   ALTER ROLE cxa1_user WITH LOGIN PASSWORD :'pw';
   ```
   An empty password var → role left as-is (so a partial re-run is safe).

The role + schema are **our own constants** (never user input), so they are interpolated straight into the SQL; only the password is a client variable. After step 8, `cxa1_user` can connect on the POOLED endpoint with the password that matches the app's `CYPHERXA1_DATABASE_URL` DSN, and its default `search_path` resolves the `cypherx_a1` schema without a per-query `options=-c search_path=…`.

**Mount.** For `migrate.sh` to see the cypherx-a1 migrations, the `migrate` service mounts them read-only at `/migrations/cypherx-a1`:

```yaml
volumes:
  - ./migrate.sh:/migrate.sh:ro
  - ../../Shared Core/auth/db/migrations:/migrations/auth:ro
  - ../../Shared Core/llms/db/migrations:/migrations/llms:ro
  - ../../Shared Core/guardrails/db/migrations:/migrations/guardrails:ro
  - ../../xAgent/ax-1/db/migrations:/migrations/xagent:ro
  - ../../Shared Core/rag/db/migrations:/migrations/rag:ro
  - ../../Shared Core/memory/db/migrations:/migrations/memory:ro
  - ../../Tools/tool-registry/db/migrations:/migrations/tool-registry:ro
  - ../../CoreProjects/cypherx-a1/db/migrations:/migrations/cypherx-a1:ro
```

The `migrate` env block also carries `CYPHERXA1_DB_PASSWORD` alongside the other `*_DB_PASSWORD` vars.

> **Run it:** `docker compose --profile migrate up migrate` (run from `infra/compose/`). Idempotent; exits 0. The console prints `[8/9] provision runtime-role passwords + search_path` with a per-role line — look for `-> cxa1_user: password set + search_path=cypherx_a1`.

### 2.3 The cross-team `postgres-init.sql` addition (deps-only stack)

`infra/dev/local/seed/postgres-init.sql` is the **deps-only** local stack's bootstrap (Tilt/kind, which *does* run a `pgvector/pgvector:pg16` postgres container — unlike compose, which uses external Neon). It mirrors the cloud Terraform bootstrap so service code written locally works unchanged in dev/staging/prod. It is executed once on first init of an empty data dir.

cypherx-a1 was added to its **closed enumeration** of `(svc, schema, runtime_user, ddl_user)` rows:

```sql
('cypherx-a1', 'cypherx_a1', 'cxa1_user',   'cxa1_ddl')
```

The surrounding `DO $$ … $$` loop then, for that row, idempotently:
- creates `cxa1_user` (LOGIN, no CREATEROLE/superuser) and `cxa1_ddl` (LOGIN + CREATEROLE), both with the throwaway `CYPHERX_LOCAL_DB_PASSWORD` (default `localdev`);
- `ALTER SCHEMA cypherx_a1 OWNER TO cxa1_ddl;`
- grants `CREATE, USAGE` to `cxa1_ddl`, `USAGE` to `cxa1_user`;
- sets `ALTER DEFAULT PRIVILEGES` so Atlas-created (owned-by-`cxa1_ddl`) tables/sequences are usable by `cxa1_user`.

> **Gotcha:** editing `postgres-init.sql` requires `docker compose -f infra/dev/local/docker-compose.yml down -v` before it re-runs — it only executes on a first/empty data dir.

### 2.4 The cross-team `postgres-bootstrap/main.tf` addition (cloud)

`infra/modules/postgres-bootstrap/main.tf` is the Terraform-owned, run-once-per-environment cloud bootstrap (Component 16). It owns the database, schemas, runtime users, DDL users, extensions, and default-privilege grants. Atlas (per service, a K8s Job) owns tables/columns/indexes/RLS *within* each schema — not this module.

cypherx-a1 was added to the module's `local.services` map (also a **closed enumeration**):

```hcl
"cypherx-a1" = { schema = "cypherx_a1", runtime_user = "cxa1_user", ddl_user = "cxa1_ddl" }
```

The map **key** (`"cypherx-a1"`) is the canonical Doppler service name; the module looks up `var.runtime_passwords["cypherx-a1"]` / `var.ddl_passwords["cypherx-a1"]`, which resolve to Doppler paths **`db/cypherx-a1/runtime_password`** and **`db/cypherx-a1/ddl_password`**. **Those two Doppler secrets must exist before `terraform apply`** or the apply fails. The `for_each = local.services` resources then create the runtime role (`cxa1_user`, least-priv), the DDL role (`cxa1_ddl`, with cluster-wide `CREATEROLE` — a documented Postgres limitation), the `cypherx_a1` schema owned by `cxa1_ddl`, the schema grants, and the default privileges.

> **Why both enumerations had to be edited:** the deps-only seed and the cloud module both hard-code the full service list. Adding a new consuming app that needs its own schema/role means adding exactly one row to each, with identical naming (`schema=cypherx_a1`, `runtime_user=cxa1_user`, `ddl_user=cxa1_ddl`) so all three environments produce the same object names.

---

## 3. The `auth.service_acl` seed (the canonical-columns rule)

`db/migrations/20260614_0002__seed.sql` seeds the service-to-service authorization edges that let cypherx-a1 mint Contract-12 service tokens for each SharedCore service it calls. It is seeded **here**, in cypherx-a1's own migration — *not* in `Shared Core/auth` — to keep auth untouched. Because the compose `migrate` job applies cypherx-a1 **after** auth, `auth.service_acl` already exists when this runs.

The insert uses the **canonical columns** `(caller_service, target_service, allowed_scopes)` and is guarded on the table existing + idempotent via `ON CONFLICT`:

```sql
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = 'auth' AND table_name = 'service_acl'
  ) THEN
    INSERT INTO auth.service_acl (caller_service, target_service, allowed_scopes) VALUES
      ('cypherx-a1', 'auth-service',       ARRAY['internal:read']),
      ('cypherx-a1', 'llms-gateway',       ARRAY['internal:read','internal:write']),
      ('cypherx-a1', 'guardrails-service', ARRAY['internal:read','internal:write']),
      ('cypherx-a1', 'rag-service',        ARRAY['internal:read','internal:write']),
      ('cypherx-a1', 'memory-service',     ARRAY['internal:read','internal:write'])
    ON CONFLICT (caller_service, target_service) DO NOTHING;
  END IF;
END
$$;
```

| `caller_service` | `target_service` | `allowed_scopes` | Why |
| --- | --- | --- | --- |
| `cypherx-a1` | `auth-service` | `internal:read` | Mint service tokens, read JWKS. |
| `cypherx-a1` | `llms-gateway` | `internal:read`, `internal:write` | Extraction chat + copilot answers (write = the metered call). |
| `cypherx-a1` | `guardrails-service` | `internal:read`, `internal:write` | Pre/post copilot screening. |
| `cypherx-a1` | `rag-service` | `internal:read`, `internal:write` | KB query (read) + ingest (write). |
| `cypherx-a1` | `memory-service` | `internal:read`, `internal:write` | Copilot episodic memory read/write. |

The scopes use the platform's `internal:read` / `internal:write` convention (same as the xagent edges); each target maps `internal:read` → its read scope (`rag:query`, `mem:read`) and `internal:write` → its write scope (`rag:ingest`, `mem:write`). The `caller_service` value **must exactly equal** `service_principal_name` (`cypherx-a1`, presented to Auth as `X-Service-Name` — see `core/config.py`).

### ⚠️ Avoid the rag-seed bug — use the canonical columns

The auth schema defines `auth.service_acl` with columns **`(caller_service, target_service, allowed_scopes)`**. cypherx-a1's seed uses exactly those.

The `rag-service` seed (`Shared Core/rag/db/migrations/20260611_0002__seed.sql`) is a **known, tolerated bug**: it inserts into the wrong column names —

```sql
-- BUGGY (rag): wrong column names — errors against the real auth schema
INSERT INTO auth.service_acl (source_service, target_service, scopes)
VALUES ('rag-service', 'llms-gateway', ARRAY['internal:read','internal:write']), ...
ON CONFLICT DO NOTHING;
```

`source_service`/`scopes` do not exist on `auth.service_acl`; the `DO $$` guard only checks the table *exists*, not column compatibility, so against the real schema this INSERT errors on the unknown columns. **cypherx-a1 must not copy that pattern.** The rule (enforced as a guard in cypherx-a1's `CLAUDE.md`): the `auth.service_acl` seed uses `(caller_service, target_service, allowed_scopes)`, never the rag-seed's `(source_service, scopes)`. cypherx-a1's `ON CONFLICT (caller_service, target_service) DO NOTHING` is also tighter (named conflict target) than rag's bare `ON CONFLICT DO NOTHING`.

> Note: nothing tenant-scoped is seeded. Connectors, `resource_acls`, and `rag_kbs` bindings are created per-tenant at **runtime** via the API.

---

## 4. The compose service blocks

Both containers are defined in `infra/compose/docker-compose.yml` under the **CoreProjects** section. Every app service listens on **`8080` in-container** and is addressed by its compose DNS name on the `cypherx` network; host port-forwards are for humans only.

### 4.1 `cypherx-a1` (host `8093` → in-container `8080`)

```yaml
cypherx-a1:
  build:
    context: ../../CoreProjects/cypherx-a1
    dockerfile: Dockerfile
  image: cypherx/cypherx-a1:local
  container_name: cypherx-a1
  networks: [cypherx]
  environment:
    PORT: "8080"
    HOST: "0.0.0.0"
    DATABASE_URL: ${CYPHERXA1_DATABASE_URL}                      # role cxa1_user, Neon POOLED, search_path=cypherx_a1
    KAFKA_BROKERS: ${KAFKA_BROKERS:-redpanda:29092}
    VALKEY_URL: ${VALKEY_URL:-redis://valkey:6379}
    AUTH_JWKS_URL: ${AUTH_JWKS_URL:-http://auth-service:8080/.well-known/jwks.json}
    AUTH_ISSUER_URL: ${AUTH_ISSUER_URL:-http://auth-service:8080}
    AUTH_PLATFORM_AUDIENCE: ${AUTH_PLATFORM_AUDIENCE:-cypherx-platform}
    AUTH_SERVICE_URL: ${AUTH_SERVICE_URL:-http://auth-service:8080}
    SERVICE_PRINCIPAL_NAME: ${CYPHERXA1_SERVICE_PRINCIPAL_NAME:-cypherx-a1}
    SERVICE_BOOTSTRAP_SECRET: ${SERVICE_BOOTSTRAP_SECRET_CYPHERXA1:-local-dev-cypherxa1-secret}
    LLMS_GATEWAY_URL: ${LLMS_GATEWAY_URL:-http://llms-gateway:8080}
    GUARDRAILS_SERVICE_URL: ${GUARDRAILS_SERVICE_URL:-http://guardrails-service:8080}
    RAG_SERVICE_URL: ${RAG_SERVICE_URL:-http://rag:8080}
    MEMORY_SERVICE_URL: ${MEMORY_SERVICE_URL:-http://memory:8080}
    TOOL_REGISTRY_URL: ${TOOL_REGISTRY_URL:-http://tool-registry:8080}
    CONNECTOR_MODE: ${CYPHERXA1_CONNECTOR_MODE:-mock}            # keyless default: replay bundled GitHub fixtures
    GITHUB_TOKEN: ${GITHUB_TOKEN:-}
    GITHUB_WEBHOOK_SECRET: ${GITHUB_WEBHOOK_SECRET:-local-dev-webhook-secret}
    RAG_EMBEDDING_MODEL: ${CYPHERXA1_RAG_EMBEDDING_MODEL:-text-embedding-3-small}   # PINNED model, never the 'embed' alias
    ENVIRONMENT: ${ENVIRONMENT:-local}
    OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-}
  ports:
    - "8093:8080"
  depends_on:
    redpanda:           { condition: service_healthy }
    valkey:             { condition: service_healthy }
    auth-service:       { condition: service_healthy }
    llms-gateway:       { condition: service_healthy }
    guardrails-service: { condition: service_healthy }
    rag:                { condition: service_healthy }
    memory:             { condition: service_healthy }
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)\""]
    interval: 15s
    timeout: 5s
    start_period: 40s
    retries: 5
```

Salient points:

- **`DATABASE_URL` ← `CYPHERXA1_DATABASE_URL`** — the Neon **POOLED** DSN for role `cxa1_user`, `sslmode=require`. (The app default in `core/config.py` is `postgresql://cxa1_user:localdev@localhost:5432/cypherx_platform`, only for host-uv dev.)
- **`SERVICE_BOOTSTRAP_SECRET` is required, no baked default** in code (`core/config.py` makes it a required field). Compose supplies it from `SERVICE_BOOTSTRAP_SECRET_CYPHERXA1` (default `local-dev-cypherxa1-secret`). It **must equal** the value Auth holds under `cypherx.service-auth.bootstrap-secrets.cypherx-a1` — see §4.3.
- **`depends_on` ordering:** deps healthy → `auth-service` → `llms-gateway` + `guardrails-service` → `rag` + `memory` → `cypherx-a1`. cypherx-a1 waits on every SharedCore service it calls (note: it does **not** depend on `tool-registry` for startup — registration is a separate, optional step).
- **Keyless defaults:** `CONNECTOR_MODE=mock` (replay bundled GitHub fixtures, no token), and it relies on upstream `MOCK_PROVIDERS`/`MOCK_EMBEDDINGS` for llms/rag.
- **`RAG_EMBEDDING_MODEL` is the explicit pinned model** (`text-embedding-3-small`, dim 1536) — never the repointable `embed` alias. The resolved model+dim are persisted immutably in `cypherx_a1.rag_kbs`.

### 4.2 `mcp-eng-memory` (host `8094` → in-container `8080`)

```yaml
mcp-eng-memory:
  build:
    context: ../../CoreProjects/cypherx-a1/mcp-eng-memory
    dockerfile: Dockerfile
  image: cypherx/mcp-eng-memory:local
  container_name: cypherx-mcp-eng-memory
  networks: [cypherx]
  environment:
    PORT: "8080"
    HOST: "0.0.0.0"
    AUTH_JWKS_URL: ${AUTH_JWKS_URL:-http://auth-service:8080/.well-known/jwks.json}
    AUTH_ISSUER_URL: ${AUTH_ISSUER_URL:-http://auth-service:8080}
    AUTH_PLATFORM_AUDIENCE: ${AUTH_PLATFORM_AUDIENCE:-cypherx-platform}
    VALKEY_URL: ${VALKEY_URL:-redis://valkey:6379}
    CYPHERXA1_BASE_URL: ${CYPHERXA1_BASE_URL:-http://cypherx-a1:8080}
    ENVIRONMENT: ${ENVIRONMENT:-local}
    OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-}
  ports:
    - "8094:8080"
  depends_on:
    auth-service: { condition: service_healthy }
    cypherx-a1:   { condition: service_healthy }
  healthcheck:
    test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/livez',timeout=3).status==200 else 1)\""]
    interval: 15s
    timeout: 5s
    start_period: 20s
    retries: 5
```

Salient points:

- **No DB env, no Kafka env.** mcp-eng-memory is **stateless by design** — it holds no DB / Kafka / outbox. It verifies the inbound JWT (JWKS) and proxies to cypherx-a1's `/v1/graph/*` + `/v1/copilot/*` via `CYPHERXA1_BASE_URL` (compose DNS `http://cypherx-a1:8080`).
- **Valkey-free revocation:** although `VALKEY_URL` is wired, the facade does not enforce revocation itself — revocation is enforced at the cypherx-a1 backend it forwards to.
- **`depends_on`** auth + cypherx-a1 only.
- Per-invocation **metering is the caller's** (the invoking xAgent's outbox), never this tool's. cypherx-a1 meters its *own* usage on its own topic (`cypherx.cypherxa1.usage.recorded`).

### 4.3 The Auth bootstrap secret for `cypherx-a1`

For cypherx-a1 to mint a Contract-12 service token, Auth must recognize its bootstrap secret. The `auth-service` block injects per-service bootstrap secrets via `SPRING_APPLICATION_JSON` with **lowercase** keys under `cypherx.service-auth.bootstrap-secrets.<service>` (the empty Spring profile does not bake these, and Spring won't reliably bind a hyphenated/uppercase env-var map key). The `cypherx-a1` entry was added alongside `xagent`/`llms`/`guardrails`:

```yaml
SPRING_APPLICATION_JSON: >-
  {"cypherx":{"service-auth":{"bootstrap-secrets":{
  "xagent":"${SERVICE_BOOTSTRAP_SECRET_XAGENT:-local-dev-xagent-secret}",
  "llms":"${SERVICE_BOOTSTRAP_SECRET_LLMS:-local-dev-llms-secret}",
  "guardrails":"${SERVICE_BOOTSTRAP_SECRET_GUARDRAILS:-local-dev-guardrails-secret}",
  "cypherx-a1":"${SERVICE_BOOTSTRAP_SECRET_CYPHERXA1:-local-dev-cypherxa1-secret}"}}}}
```

The map key `cypherx-a1` is exactly the `X-Service-Name` cypherx-a1 sends; the value **must match** the cypherx-a1 container's `SERVICE_BOOTSTRAP_SECRET` (both default to `local-dev-cypherxa1-secret` from `${SERVICE_BOOTSTRAP_SECRET_CYPHERXA1}`). With the secret recognized **and** the `auth.service_acl` edges seeded (§3), cypherx-a1's `POST /v1/service-tokens` exchange succeeds and the minted token's allow-list covers `auth-service`/`llms-gateway`/`guardrails-service`/`rag-service`/`memory-service`.

> Three things must line up for a working service-token mint: (1) the **bootstrap secret** in Auth's `SPRING_APPLICATION_JSON` matches the app's `SERVICE_BOOTSTRAP_SECRET`; (2) the **`auth.service_acl` edges** are seeded with the canonical columns; (3) `service_principal_name` / `X-Service-Name` / `caller_service` are all the literal string `cypherx-a1`.

### 4.4 The `.env` keys cypherx-a1 introduces

`infra/compose/.env.example` gains these (only the DSN/password are real-Neon placeholders; the rest are LOCAL-ONLY throwaways):

| Env key | Used by | Notes |
| --- | --- | --- |
| `CYPHERXA1_DATABASE_URL` | `cypherx-a1` → `DATABASE_URL` | Neon **POOLED**, role `cxa1_user`, `sslmode=require`, `search_path=cypherx_a1`. **Set a real value.** |
| `CYPHERXA1_DB_PASSWORD` | `migrate` job (`set_role_pw cxa1_user`) | The password set on `cxa1_user`; must match the password embedded in `CYPHERXA1_DATABASE_URL`. **Set a real value.** |
| `SERVICE_BOOTSTRAP_SECRET_CYPHERXA1` | `cypherx-a1` + `auth-service` | Local throwaway (`local-dev-cypherxa1-secret`); must be identical on both. |
| `CYPHERXA1_SERVICE_PRINCIPAL_NAME` | `cypherx-a1` | Defaults to `cypherx-a1`; do not change locally. |
| `CYPHERXA1_CONNECTOR_MODE` | `cypherx-a1` → `CONNECTOR_MODE` | `mock` (keyless default) or `live`. |
| `CYPHERXA1_RAG_EMBEDDING_MODEL` | `cypherx-a1` → `RAG_EMBEDDING_MODEL` | Pinned model; default `text-embedding-3-small`. |
| `CYPHERXA1_BASE_URL` | `mcp-eng-memory` | Defaults to `http://cypherx-a1:8080`. |
| `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` | `cypherx-a1` | Only needed for `live` connector mode / signed webhooks. |

---

## 5. Port map

cypherx-a1 occupies the next two free host ports after the existing platform (`8080`–`8091` were taken; `8092` is `frontend-bff`). Every app service is `8080` in-container.

| Service | In-container | Host | In-network URL |
| --- | --- | --- | --- |
| **cypherx-a1** | 8080 | **8093** | `http://cypherx-a1:8080` |
| **mcp-eng-memory** | 8080 | **8094** | `http://mcp-eng-memory:8080` |

For reference, the neighbours in the host port map:

| Service | Host | Service | Host |
| --- | --- | --- | --- |
| auth-service | 8080 | rag | 8087 |
| xagent | 8083 | memory | 8088 |
| llms-gateway | 8085 | tool-registry | 8089 |
| guardrails-service | 8086 | tool-web-search | 8091 |
| edge (Caddy) | 8000 | frontend-bff | 8092 (→8088) |
| demo (profile) | 8090 | frontend-app (SPA) | 3000 |

> The `8093`/`8094` choice is deliberate: `8090` is the demo BFF, `8091` is tool-web-search, `8092` is frontend-bff. `8093`/`8094` are the first free slots and keep cypherx-a1's two containers adjacent.

---

## 6. Tool-registry registration (`mcp-eng-memory@1.0.0`)

`mcp-eng-memory` is the MCP server an AI coding agent (or xAgent) talks to. It exposes its tool catalogue via `manifest.json` (validated against `contracts/mcp/manifest.schema.json`, Contract 4). The manifest declares:

| Field | Value |
| --- | --- |
| `name` | `mcp-eng-memory` |
| `version` | `1.0.0` |
| `protocol_version` | `mcp/1.0` |
| `auth_required` | `true` |
| `required_scopes` | `tool:invoke`, `tool:mcp-eng-memory:invoke` |
| `health_endpoint` | `/livez` |
| `metrics_endpoint` | `/metrics` |

**Tools exposed** (each `idempotent: true`):

| Tool | Backs | Required input | LLM? |
| --- | --- | --- | --- |
| `who_owns` | `/v1/graph/who-owns` | `target` | no |
| `why_built` | `/v1/graph/why-built` | `feature` | no |
| `what_breaks_if_changed` | `/v1/graph/what-breaks` | `target`, `max_hops` (1–6, default 3) | no |
| `experts_on` | `/v1/graph/experts` | `topic` | no |
| `graph_neighbors` | `/v1/graph/*` | `target`, `max_hops` (1–4, default 2) | no |
| `incident_root_cause` | copilot | `incident` | yes (60s) |
| `how_does_x_work` | copilot | `topic` | yes (60s) |

The graph tools proxy cypherx-a1's read-only, cited `/v1/graph/*` endpoints (e.g. `POST /v1/graph/who-owns`, `/v1/graph/what-breaks`, `/v1/graph/experts`, `/v1/graph/why-built`); the two LLM tools proxy the cited copilot flow. The same backing logic serves both the public REST API and MCP agents.

**Registration is a deliberate, separate step — not a compose dependency.** The tool-registry no longer seeds any platform tool at startup — every tool (platform or tenant) is registered through the API by its owner (e.g. the public `web_search` flow-tool is bootstrapped by tool-flow-bridge). To register `mcp-eng-memory`, an operator (or a one-shot job) `POST`s the manifest to the registry:

```bash
# Mint/obtain an admin/agent JWT with tool:register, then:
curl -fsS -X POST http://localhost:8089/v1/tools \
  -H "Authorization: Bearer $REGISTRY_JWT" \
  -H "Content-Type: application/json" \
  --data @CoreProjects/cypherx-a1/mcp-eng-memory/manifest.json
```

The registry resolves the tool as `mcp-eng-memory@1.0.0` (from `name` + `version`) and health-polls it at its `health_endpoint` (`/livez`, in-network `http://mcp-eng-memory:8080/livez`). After registration, xAgent's tool loop can `resolve` `mcp-eng-memory@1.0.0` and invoke a tool via `POST /mcp/v1/invoke` on the MCP server.

> **Metering invariant:** when xAgent invokes a `mcp-eng-memory` tool, the per-invocation `cypherx.tools.invocation.metered` event is emitted by **xAgent's outbox**, never by the stateless tool. cypherx-a1 separately meters its *own* downstream usage (llms/rag/memory) on `cypherx.cypherxa1.usage.recorded` (Contract 19 — units + `request_id`, never rewriting the gateway's cost).

---

## 7. Running locally, keyless

cypherx-a1 runs fully offline with no API keys. The keyless toggles:

| Toggle | Default | Effect |
| --- | --- | --- |
| `CONNECTOR_MODE` | `mock` | Replay bundled GitHub fixtures (no `GITHUB_TOKEN`). Fixtures carry explicit `owns`/`depends_on` edges so `who_owns`/`what_breaks` work without an LLM. |
| `MOCK_PROVIDERS` (llms) | `true` | llms-gateway returns deterministic mock chat/embeddings. |
| `MOCK_EMBEDDINGS` (rag) | `true` | rag embeds in-process (no llms call). |
| `CLASSIFIER_MODE` (guardrails) | `stub` | Deterministic guardrail decisions. |

### 7.1 First-run sequence

Run everything from `infra/compose/` (so `.env` is picked up):

```bash
# 1. Configure env — fill the Neon DSNs (POOLED for apps, DIRECT for migrate) + passwords incl.
#    CYPHERXA1_DATABASE_URL (POOLED, role cxa1_user) and CYPHERXA1_DB_PASSWORD.
cp .env.example .env
$EDITOR .env

# 2. One-time schema/role/RLS/seed against Neon DIRECT — applies cypherx-a1 LAST (after auth),
#    creates schema cypherx_a1 + role cxa1_user, seeds auth.service_acl, provisions the cxa1_user
#    password + search_path=cypherx_a1.
docker compose --profile migrate up migrate

# 3. Bring up the stack (or just the two new services + their deps).
docker compose up -d --build cypherx-a1 mcp-eng-memory
#   or the whole platform:
docker compose up -d --build
```

`depends_on` + healthchecks order startup: deps → auth → llms+guardrails → rag+memory → cypherx-a1 → mcp-eng-memory.

### 7.2 Smoke-check it is up

```bash
# Process-only liveness (no deps):
curl -fsS http://localhost:8093/livez        # cypherx-a1
curl -fsS http://localhost:8094/livez        # mcp-eng-memory

# Readiness (Postgres reachable + warm Auth JWKS). 503 until Neon is reachable — cold start tolerated.
curl -fsS http://localhost:8093/readyz

# MCP manifest (served by the facade):
curl -fsS http://localhost:8094/.well-known/mcp/manifest.json   # or the manifest route the server exposes
```

A typical keyless end-to-end check: `POST /v1/connectors/github/sync` (ingests fixtures into the graph) → `POST /v1/graph/who-owns` (cited, no LLM) → `POST /v1/copilot/ask` (guardrails-screened, mock-LLM answer with citations).

### 7.3 Going live (flip a toggle, supply a key)

| To enable | Set | And supply |
| --- | --- | --- |
| Real GitHub ingestion | `CYPHERXA1_CONNECTOR_MODE=live` | `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` |
| Real LLM extraction/copilot | `MOCK_PROVIDERS=false` (llms) | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` on `llms-gateway` |
| Real embeddings | `MOCK_EMBEDDINGS=false` (rag) | provider key on `llms-gateway` (rag embeds via llms) |

The pinned `RAG_EMBEDDING_MODEL` (`text-embedding-3-small`, dim 1536) does not change when you go live — KBs are created with the explicit model and persisted immutably in `rag_kbs`.

---

## 8. Deployment summary & checklist

**Local compose (Neon, no pg container):**
- [ ] `migrate.sh` list includes `cypherx-a1` (last) and the `migrate` service mounts `…/cypherx-a1/db/migrations:/migrations/cypherx-a1:ro`.
- [ ] `migrate.sh` step 8 has `set_role_pw cxa1_user cypherx_a1 "${CYPHERXA1_DB_PASSWORD:-}"`.
- [ ] `.env` sets `CYPHERXA1_DATABASE_URL` (POOLED, `cxa1_user`, `sslmode=require`) + `CYPHERXA1_DB_PASSWORD` (matching).
- [ ] Auth's `SPRING_APPLICATION_JSON` has the `"cypherx-a1"` bootstrap-secret key = the app's `SERVICE_BOOTSTRAP_SECRET`.
- [ ] `auth.service_acl` edges seeded with `(caller_service, target_service, allowed_scopes)` (NOT the rag-seed columns).
- [ ] Compose blocks `cypherx-a1` (`8093→8080`) + `mcp-eng-memory` (`8094→8080`) present with correct `depends_on`.

**Local deps-only (Tilt/kind, pg container):**
- [ ] `infra/dev/local/seed/postgres-init.sql` enumerates `('cypherx-a1','cypherx_a1','cxa1_user','cxa1_ddl')`. (Re-seed needs `down -v`.)

**Cloud (AWS RDS / EKS):**
- [ ] `infra/modules/postgres-bootstrap/main.tf` `local.services` has `"cypherx-a1" = { schema = "cypherx_a1", runtime_user = "cxa1_user", ddl_user = "cxa1_ddl" }`.
- [ ] Doppler secrets `db/cypherx-a1/runtime_password` and `db/cypherx-a1/ddl_password` exist (apply fails otherwise).
- [ ] Atlas applies `…__init.sql` + `…_0002__seed.sql` for the `cypherx_a1` schema; tool-registry has `mcp-eng-memory@1.0.0` registered.

**Invariants that are not bugs (do not "fix"):**
- `outbox` has **no RLS** (cross-tenant publish queue; isolation in the payload).
- The runtime role `cxa1_user` **cannot `CREATE EXTENSION`** (frozen `pgvector/pgvector:pg16` image; adjacency-list + recursive-CTE graph is mandatory — no Apache AGE).
- The `auth.service_acl` seed lives in cypherx-a1's migration (keeps auth untouched) and uses the **canonical columns**.
- `mcp-eng-memory` is **stateless** (no DB/Kafka/outbox); metering belongs to the caller.
- cypherx-a1 is a **consuming app** under `CoreProjects/`, not a SharedCore service — placement is load-bearing for build contexts and the bootstrap enumerations.
