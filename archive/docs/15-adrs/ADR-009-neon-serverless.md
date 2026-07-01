# ADR-009 · Neon Serverless Postgres for Local Development and Staging

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX needs a Postgres instance for local development and staging that supports `pgvector` (for RAG and memory embedding storage), can be provisioned without a running Docker container (removing Postgres from the compose stack to keep it lighter), and ideally supports database branching for test isolation. The compose stack already runs Redpanda, Valkey, and MinIO as containers; adding a heavyweight Postgres container (especially one with pgvector extension pre-installed) increases cold-start time, memory requirements, and the risk of local data persistence issues. Separately, running a Postgres container in compose creates a divergence risk: the container Postgres version and extension set may drift from the managed cloud Postgres.

## Decision

**Postgres is external to the compose stack — there is no postgres container.** All environments (local developer machines, staging, and production) use **Neon serverless Postgres**. Developers create a Neon project with a single database `cypherx_platform`. Neon exposes two connection string types that serve distinct roles in the platform:

- **POOLED endpoint** (hostname contains `-pooler`): PgBouncer in transaction mode. Used by all running services. Compatible with `SET LOCAL` RLS context variables (Contract 13). Required because services hold many short-lived connections.
- **DIRECT endpoint** (no `-pooler`): Direct session-mode connection. Used exclusively by the **Atlas migration job** (`--profile migrate`), which needs session-level advisory locks for safe concurrent migration execution.

Both endpoints require `sslmode=require` on every DSN — this is enforced by the charts schema validator (a `const` pattern match on the DSN). Neon's database branching feature enables ephemeral test databases cloned from the main branch without copying data physically. Cold-start latency (first connection after idle) is an accepted trade-off.

## Rationale

### Why This

Neon provides a fully managed Postgres-compatible serverless database with `pgvector` (and `pgcrypto`) pre-installed, accessible from any developer machine or CI runner via a connection string — no cluster management, no Docker volume management, no pg_hba.conf editing. The serverless scaling model means a developer's idle local stack does not pay for idle Postgres compute. The single external database connection string is the configuration primitive, which is simpler to reason about than a container with data volumes that may get stale or corrupted.

The POOLED / DIRECT split is a nuanced but important decision: PgBouncer transaction mode (POOLED) is required for high-concurrency service connections, but it breaks `SET` (session-level) commands. Using `SET LOCAL` inside transactions for RLS context (Contract 13) is compatible with transaction mode. The Atlas migration tool, however, requires session-level advisory locks (`pg_advisory_lock`) which do not survive PgBouncer transaction boundaries — hence the migration job always uses the DIRECT endpoint.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Postgres container in Docker Compose | Requires each developer to manage data volumes; pgvector extension must be manually installed or pre-baked in a custom image; version drift between compose container and cloud managed Postgres; heavy memory footprint (~150 MB baseline). |
| Supabase | Postgres-compatible, includes pgvector; but Supabase's local dev stack is itself a multi-container compose setup (Kong, GoTrue, PostgREST, Studio) that adds even more containers. The simplicity goal is defeated. |
| PlanetScale | MySQL-based (with Vitess); no pgvector; no RLS equivalent; incompatible with Atlas Postgres migrations. |
| AWS RDS PostgreSQL | Fully managed, production-grade; but requires a VPC even for dev/staging, no serverless cold-start savings, no database branching for test isolation, and requires AWS credentials for every developer. Neon is provider-agnostic via connection string. |
| CockroachDB Serverless | Distributed SQL, globally available; but limited pgvector support, different SQL dialect quirks, and RLS requires careful adaptation. Premature optimization for the current scale. |
| Local Postgres via Homebrew / system package | Works for individuals but creates version fragmentation across the team; pgvector installation varies by OS; no branching; breaks the "clone repo, copy .env, run compose" onboarding goal. |

## Consequences

### Positive

- Zero Postgres containers in compose: `docker compose up` starts faster and uses less RAM.
- `pgvector` and `pgcrypto` are pre-installed on Neon — no custom Docker image or manual extension installation needed.
- Database branching allows creating an exact copy of the dev database in seconds for integration test isolation, then deleting it after the test run — no test-data contamination.
- Single connection string as the configuration primitive: onboarding a new developer is `cp .env.example .env` + fill in the Neon DSN. No Docker volume management, no pg_hba editing.
- Neon's PITR (point-in-time recovery) and managed HA eliminate the operational burden of Postgres backup/restore in dev and staging.
- Serverless autoscale: Neon pauses the compute after 5 minutes of inactivity, reducing cost during off-hours development.
- `sslmode=require` is enforced on every DSN by the charts schema validator — TLS is on everywhere, not just in production.

### Negative / Trade-offs

- **Cold-start latency**: the first connection after Neon's compute has been idle takes 300–800 ms to resume. `/readyz` is designed to return 503 until the pool is warm, absorbing this window. The stack's health-check ordering (`depends_on` with condition `service_healthy`) prevents services from starting traffic before their DB connections are established.
- **External network dependency in local dev**: unlike a local Postgres container, Neon requires an internet connection. Offline development is not possible. Accepted trade-off given modern always-connected developer environments.
- **Connection string secret management**: real Neon DSNs (with passwords) must never be committed. The `.env.example` contains `<<< SET REAL NEON VALUE >>>` placeholders; CI rejects committed `.env` files. Developers manually fill in values once.
- **Pooled vs. Direct DSN discipline**: developers and CI pipelines must use the correct endpoint for each purpose. Using the POOLED endpoint for migrations causes advisory lock failures; using the DIRECT endpoint for services causes connection exhaustion. This is documented in `infra/compose/LOCAL_RUN_NOTES.md` and enforced by variable naming in `.env.example`.
- **Neon free tier limits**: the free tier has 0.5 CPU compute hours/month and 3 GB storage. Teams doing sustained local development or large RAG ingestion may hit compute limits; a paid Neon plan is recommended for sustained platform development.
