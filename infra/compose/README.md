# CypherX — full-stack Docker orchestration (`infra/compose`)

One compose file brings up **every CypherX service** plus its backing dependencies. **Postgres is EXTERNAL**
(Neon serverless) — there is **no postgres container**; services read their DB DSN from env. Redpanda (Kafka),
Valkey, and MinIO **do** run as containers.

```
infra/compose/
├── docker-compose.yml   # the full stack (deps + app services; demo & migrate profiles)
├── migrate.sh           # one-shot schema/role/RLS/seed apply (run by the migrate profile)
├── .env.example         # every variable the compose references (copy to .env)
└── README.md            # this file
```

Run all commands from the `infra/compose/` directory (so `.env` is picked up automatically), e.g.
`docker compose --profile demo config`.

---

## (a) Set up Neon

1. Create a Neon project with a database named **`cypherx_platform`**.
2. Note the two endpoint hostnames Neon gives you:
   - **POOLED** host — contains `-pooler` (transaction mode). Used by the **app services**.
   - **DIRECT** host — no `-pooler` (session mode). Used by the **migrate** job (needs session-level advisory locks).
3. Create the per-service login roles (`auth_user`, `llms_user`, `grd_user`, `xagent_user`) and an owner/admin role,
   or let the migration `*__init.sql` create the runtime roles (they do, idempotently) and just set their passwords.
4. `sslmode=require` is **mandatory** on every Neon DSN. Neon auto-suspends idle compute, so the first call after
   idle may be slow — the services' fail-soft pool-open + retries already tolerate this.

For the full Neon migration runbook, see the project migration runbook
(`docs/` migration runbook / the `db/migrations` README in each service repo). Each service's
`db/migrations/*__init.sql` creates its own schema + runtime role + tables + RLS and is idempotent.

## (b) Configure `.env`

```bash
cp .env.example .env
# then edit .env and fill every value marked  <<< SET REAL NEON VALUE >>>
```

The variables you **MUST** set to real Neon values:

| Variable                | Endpoint | Schema / role     | Used by        |
|-------------------------|----------|-------------------|----------------|
| `AUTH_DATABASE_URL`     | POOLED   | `auth` (JDBC `currentSchema=auth`) | auth-service |
| `AUTH_DB_PASSWORD`      | —        | password for `auth_user` | auth-service |
| `LLMS_DATABASE_URL`     | POOLED   | `llms` / `llms_user` | llms-gateway |
| `GUARDRAILS_DATABASE_URL`| POOLED  | `guardrails` / `grd_user` | guardrails-service |
| `XAGENT_DATABASE_URL`   | POOLED   | `xagent` / `xagent_user` | xagent |
| `MIGRATE_DATABASE_URL`  | **DIRECT** | owner/admin role | migrate job |
| `DEMO_DB_URL`           | POOLED or DIRECT | owner/admin role | demo BFF (sentinel reset) |

Everything else in `.env.example` has a safe local default (auth AES key, bootstrap token, per-service bootstrap
secrets, MinIO creds, `MOCK_PROVIDERS=true`, `CLASSIFIER_MODE=stub`, etc.) and can be left as-is for local dev.

> Auth note: this stack runs auth with **`SPRING_PROFILES_ACTIVE=""`** (the base, env-driven profile) so the Neon
> DSN actually takes effect. `application-local.yaml` hardcodes `localhost` and would otherwise override the env.
> Because the default profile does not bake the AES key / bootstrap token / per-service bootstrap secrets, those are
> supplied explicitly from `.env` (copied from `application-local.yaml`; LOCAL-ONLY throwaway values).

## (c) Run the migrations **once** (first setup, before the app services)

```bash
docker compose --profile migrate up migrate
```

This applies, in order, against `MIGRATE_DATABASE_URL` (the **DIRECT** endpoint):
`CREATE EXTENSION pgcrypto, vector` → then for `auth`, `llms`, `guardrails`, `xagent`: each `*__init.sql`
(schema + role + tables + RLS) then each `*__seed.sql`. The scripts are idempotent, so re-running is safe. The job
exits 0 when done.

## (d) Bring up the stack

```bash
docker compose up -d --build
```

Starts the backing deps (redpanda, valkey, minio + minio-init) and the four backend app services
(auth-service, llms-gateway, guardrails-service, xagent). `depends_on` + healthchecks order startup:
deps become healthy → auth → llms-gateway + guardrails-service → xagent. Watch with:

```bash
docker compose ps
docker compose logs -f auth-service
```

Readiness probes: `/readyz` reports `503` until Neon is reachable (cold-start tolerated); `/livez` is process-only.

## (e) The demo profile (opt-in)

```bash
docker compose --profile demo up -d --build demo
```

Adds the demo BFF (depends on auth + xagent + llms + guardrails). It listens on **8090 in-container** (it predates
the canonical 8080 rule) and maps to host `8090`. On boot it does a best-effort bootstrap-sentinel reset via `psql`
against `DEMO_DB_URL` (gated by `DEMO_RESET_BOOTSTRAP=1`).

Tear down (keep volumes): `docker compose --profile demo down`  •  wipe volumes too: add `-v`.

## (f) Canonical port map

| Service             | In-container port | Host port | In-network URL                       |
|---------------------|-------------------|-----------|--------------------------------------|
| auth-service        | 8080              | **8080**  | `http://auth-service:8080`           |
| xagent              | 8080              | **8083**  | `http://xagent:8080`                 |
| llms-gateway        | 8080              | **8085**  | `http://llms-gateway:8080`           |
| guardrails-service  | 8080              | **8086**  | `http://guardrails-service:8080`     |
| demo (profile)      | 8090              | **8090**  | `http://demo:8090`                   |
| redpanda (Kafka)    | 29092 internal    | 9092      | `redpanda:29092` (in-network)        |
| redpanda admin      | 9644              | 9644      |                                      |
| redpanda schema-reg | 8081              | 8081      |                                      |
| valkey              | 6379              | 6379      | `redis://valkey:6379`                |
| minio (S3 API)      | 9000              | 9000      | `http://minio:9000`                  |
| minio console       | 9001              | 9001      |                                      |

## (g) Neon notes

- **`sslmode=require` is mandatory** on every Neon DSN.
- **Pooled vs direct**: app services use the **POOLED** endpoint (transaction mode — compatible with the
  Contract-13 `SET LOCAL` RLS). The **migrate** job uses the **DIRECT** endpoint (session mode — migrations take
  session-level advisory locks the pooler cannot hold). Don't swap them.
- Neon auto-suspends idle compute: the **first** request after idle may be slow. The services' fail-soft pool-open
  and bounded DB warm tolerate the cold start; `/readyz` returns `503` until Postgres responds, then flips to ready.
