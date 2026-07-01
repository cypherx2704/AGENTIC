# CypherX — Local Development Stack (`dev/local/`)

Phase 1, Component 17c. Bring up the SharedCore + xAgent dependency graph on a single laptop **without touching AWS**.

This directory ships laptop-friendly substitutes for the cloud infrastructure, so service teams (Phase 2+) can develop
in isolation instead of fighting over a shared dev cluster:

| Cloud (AWS) | Local substitute | Image | Ports |
|---|---|---|---|
| RDS PostgreSQL 16 + pgvector | PostgreSQL + pgvector | `pgvector/pgvector:pg16` | `5432` |
| ElastiCache Valkey 7 | Valkey | `valkey/valkey:7-alpine` | `6379` |
| MSK Kafka 3.6 | Redpanda (Kafka API-compatible) | `redpandadata/redpanda` | `9092` (Kafka), `9644` (admin), `8081` (Schema Registry), `8082` (REST) |
| S3 | MinIO | `minio/minio` | `9000` (API), `9001` (console) |

> **No Istio, no Kong locally — by design (Component 17c).** Services reach each other directly via DNS
> (`http://auth-service:8080`). There is no service-mesh mTLS and no API gateway in the local stack. The cloud-only
> `ALB -> Kong` plaintext boundary and `Kong -> backend` mTLS exist only in dev/staging/prod, not here.

---

## Prerequisites

- **Docker** (Desktop or Engine) with the Compose v2 plugin (`docker compose`, not `docker-compose`).
- **Tilt** (`tilt.dev`) — for the full live-reload loop. ([install](https://docs.tilt.dev/install.html))
- **kind** — single-node Kubernetes-in-Docker, used by Tilt to run the service pods. ([install](https://kind.sigs.k8s.io/))
- Optional: **rpk** (Redpanda CLI) on the host if you want to poke Kafka directly from your shell.

You do **not** need any AWS credentials, VPN, or Doppler login to run the local stack.

---

## Fast path — dependencies only (`docker compose`)

If you just need Postgres/Valkey/Kafka/S3 up (e.g. you are writing infra, or running a service from your IDE directly),
skip Tilt entirely:

```bash
# 1. Create your local env file from the template (placeholders only — never real keys).
cp dev/local/seed/doppler.env.example dev/local/.env

# 2. Bring up the four deps. Healthchecks gate readiness; this returns in well under a minute on a warm cache.
docker compose -f dev/local/docker-compose.yml --env-file dev/local/.env up -d

# 3. Create the dev Kafka topics (idempotent).
docker compose -f dev/local/docker-compose.yml exec -T redpanda bash < dev/local/seed/kafka-topics.sh

# 4. Check health.
docker compose -f dev/local/docker-compose.yml ps
```

The Postgres init SQL (`seed/postgres-init.sql`) runs automatically on **first** boot: it creates the 7 schemas, the
per-service runtime (`*_user`) and DDL (`*_ddl`) roles, the `vector` + `pg_stat_statements` extensions, and an example
RLS table (`auth.example_agents`) so the tenant-isolation path is exercisable immediately.

Validate the compose file without starting anything:

```bash
docker compose -f dev/local/docker-compose.yml config >/dev/null && echo OK
```

---

## Full path — `tilt up`

`tilt up` brings up the dependencies, bootstraps the Kafka topics, and (as service code lands in Phase 2+) builds and
hot-reloads each SharedCore/xAgent service into a local `kind` cluster — all in **under 5 minutes** on a warm cache.

```bash
cp dev/local/seed/doppler.env.example dev/local/.env   # first time only

# Create a kind cluster on the same docker network as the compose deps, so in-cluster pods can resolve
# `postgres`, `valkey`, `redpanda`, `minio` by name (see "How services connect" below).
kind create cluster --name cypherx-local

cd dev/local
tilt up                 # open the printed UI URL (default http://localhost:10350)
```

Tilt UI groups resources:

- **`1-deps`** — Postgres, Valkey, Redpanda, MinIO, and the one-shot `redpanda-topics` bootstrap.
- **`2-shared-core`** — auth-service, llms-gateway, guardrails, memory-service, rag-service, xagent. Each block is
  **guarded**: it only activates once that service's code exists under `services/<name>/` with a `Dockerfile` and
  `deploy/local/*.yaml`. **Until Phase 2 teams add their services, `tilt up` brings up only the deps + topics** —
  which is the correct, working state for Phase 1.

Bring up **only** the dependencies (skip all service builds) even with Tilt:

```bash
tilt up -- --deps-only
```

### Service ports

Once a service's code lands, Tilt port-forwards it to the host:

| Service | localhost | In-cluster DNS | Owns (Kong routes in cloud) |
|---|---|---|---|
| auth-service | `8080` | `auth-service:8080` | `/v1/auth/*`, `/v1/agents/*`, `/v1/tokens/*`, `/v1/service-tokens` |
| llms-gateway | `8081` | `llms-gateway:8080` | `/v1/llms/*` |
| guardrails | `8082` | `guardrails-service:8080` | `/v1/guardrails/*` |
| memory-service | `8083` | `memory-service:8080` | `/v1/memory/*` |
| rag-service | `8084` | `rag-service:8080` | `/v1/rag/*` |
| xagent | `8085` | `xagent:8080` | `/v1/tasks/*`, `/v1/workflows/*` |

> Note: `/v1/agents/*` is owned by **Auth**, not xAgent (Component 8 route-ownership rule). xAgent runs agent code but
> does not own the agent identity resource.

---

## How services connect

Every service reads its connection config from environment variables (see `seed/doppler.env.example`). Locally these
point at the compose services by their docker-network DNS names:

```
POSTGRES_DSN   postgresql://<svc>_user:localdev@postgres:5432/cypherx_platform?sslmode=disable&search_path=<schema>
VALKEY_URL     redis://valkey:6379/0
KAFKA_BROKERS  redpanda:29092            # INTERNAL listener for compose/kind peers
S3_ENDPOINT    http://minio:9000         # path-style addressing (S3_FORCE_PATH_STYLE=true)
```

- **From a host shell** (e.g. `psql`, `redis-cli`, `rpk` on your machine), swap the hostname for `localhost` and use
  the published port: `psql "postgresql://cypherx_admin:localdev@localhost:5432/cypherx_platform"`.
- **From a kind pod**, the compose names resolve **only if** the kind cluster shares the compose docker network. After
  `kind create cluster`, connect its node container to the compose network once:
  ```bash
  docker network connect cypherx-local_default cypherx-local-control-plane
  ```
  (Compose creates the network `cypherx-local_default` from the `name: cypherx-local` in `docker-compose.yml`.)
- **Service-to-service**: direct DNS, e.g. xAgent calls `http://auth-service:8080`. No mesh, no gateway locally.

### Tenant isolation (RLS) is exercisable locally

`seed/postgres-init.sql` creates `auth.example_agents` with `ENABLE`/`FORCE ROW LEVEL SECURITY` and the
`USING (tenant_id = current_setting('app.tenant_id')::uuid)` policy (Contract 13). Two rows are seeded under two
tenants. Prove cross-tenant denial:

```bash
docker compose -f dev/local/docker-compose.yml exec -T postgres \
  psql -U auth_user -d cypherx_platform -c \
  "BEGIN; SET LOCAL app.tenant_id='00000000-0000-0000-0000-0000000000aa'; SELECT name FROM auth.example_agents; COMMIT;"
# -> returns ONLY 'tenant-a-agent'; the tenant-b row is invisible.
```

This mirrors `contracts/migrations/service-template` so service authors get the same RLS behaviour locally as in cloud.

---

## Resetting

```bash
# Stop services, keep data (volumes survive).
docker compose -f dev/local/docker-compose.yml down
#   ...or, under Tilt:
tilt down

# Full wipe — delete volumes so Postgres re-runs seed/postgres-init.sql on next up.
docker compose -f dev/local/docker-compose.yml down -v

# Nuke the kind cluster too.
kind delete cluster --name cypherx-local
```

> The Postgres init SQL runs **only on first init** (empty data dir). If you change `seed/postgres-init.sql`, you must
> `down -v` (or `docker volume rm cypherx-local_postgres-data`) for the change to take effect.

---

## Files

```
dev/local/
├── docker-compose.yml          # postgres, valkey, redpanda, minio (+ minio-init bucket bootstrap)
├── Tiltfile                    # deps + topic bootstrap + guarded SharedCore/xAgent service blocks
├── seed/
│   ├── postgres-init.sql       # schemas, *_user/*_ddl roles, pgvector, example RLS table
│   ├── kafka-topics.sh         # rpk topic create for the dev core topics (+ DLQs)
│   └── doppler.env.example     # placeholder env vars ONLY (never real keys)
└── README.md                   # this file
```

## Security note

Every credential in this directory is a **throwaway local dev value** (`localdev`, `cypherxlocal`, `changeme`). They
are intentionally weak and intentionally committed. **Never** put a real Doppler secret, AWS key, or LLM API key in any
file here. Real secrets live in Doppler and are synced into cloud pods by the Doppler operator (Component 11/20). Your
filled-in `dev/local/.env` is gitignored — keep it that way.
