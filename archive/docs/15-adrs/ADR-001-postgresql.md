# ADR-001 · PostgreSQL as Primary Database

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX AI is a multi-tenant agentic platform whose services collectively need: relational data with strong ACID guarantees (agent registrations, JWT signing keys, audit logs), vector similarity search for RAG knowledge bases and agent memory, enforced per-tenant row isolation, and a tractable migration story across a dozen microservices that each own their own schema. The platform is polyglot at the service layer (Kotlin, Python) but must converge on one database engine to avoid sprawl in the local compose stack, Neon configuration, and operational runbooks.

## Decision

PostgreSQL is the exclusive relational store across all CypherX services. Each service owns a dedicated Postgres schema (`auth`, `llms`, `guardrails`, `xagent`, `rag`, `memory`, `tools`, `cypherx_a1`) inside a single Neon database (`cypherx_platform`), isolated via per-schema non-superuser roles and Row-Level Security. The `pgvector` extension powers embedding storage and ANN search for `rag` and `memory`. Atlas (Ariga) manages all schema migrations. In local and staging environments the database is hosted on **Neon serverless Postgres**; cloud production uses Neon or equivalent managed Postgres with PgBouncer in transaction mode in front.

## Rationale

### Why This

PostgreSQL's extension ecosystem removes the need to introduce separate specialised stores. `pgvector` gives native ANN similarity search inside the same ACID transaction boundary used for metadata — removing the eventual-consistency gap that occurs when a document is stored in a relational DB but its vector lives in a separate service like Pinecone or Weaviate. JSONB allows semi-structured payloads (LLM request/response, event envelopes, tool definitions) without sacrificing indexability. Row-Level Security at the engine level (not application layer) enforces tenant isolation regardless of which service code path writes data — a critical safety property for a multi-tenant SaaS (see also ADR-005). Schema-per-service inside one cluster keeps operational surface small while preserving code-level isolation: each service connects with its own role that cannot see other schemas.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| MySQL / MariaDB | No native vector type; weaker JSONB support; RLS is application-level only; smaller extension ecosystem for the AI workloads we need. |
| MongoDB | Document model is flexible but loses ACID cross-collection transactions; no native vector search at the time of decision without Atlas Vector Search (separate product, cost); RLS equivalent requires application-layer filtering which is error-prone in multi-tenant contexts. |
| DynamoDB | Serverless scaling is attractive, but no joins, no JSONB operators, no RLS, and pgvector is entirely absent. Would require a separate vector store (OpenSearch, Pinecone) adding cost and operational complexity for every environment. |
| PostgreSQL + separate Pinecone/Weaviate | Keeps relational and vector in separate systems, requiring two transaction boundaries — a vector upsert can succeed while the metadata row fails (or vice versa). The Neon + pgvector combination eliminates this split. |
| CockroachDB | Distributed SQL would help global scale but has limited `pgvector` support, no RLS equivalence, and adds complexity that is premature at the current scale. Revisitable at Phase 13+ hardening. |

## Consequences

### Positive

- One Neon project, one connection pool, one migration tool, one backup/restore procedure for all services.
- `pgvector` inside ACID means embedding upserts and metadata writes are atomic — no split-brain between vector index and relational metadata.
- RLS-enforced tenant isolation is architecturally impossible to bypass from application code (services run as `NOLOGIN` roles without `BYPASSRLS`).
- Schema-per-service gives teams independent migration timelines without cross-service DDL coordination.
- JSONB with GIN indexes handles event envelopes, tool manifests, and LLM response bodies without a separate document store.
- Neon branching enables ephemeral test databases from production snapshots.

### Negative / Trade-offs

- Neon cold-start latency (first connection after idle period) adds ~300–800 ms to the first request after idle. Accepted: `/readyz` returns 503 until the pool is warm; load-balancer health checks absorb the window.
- pgvector ANN recall is approximate and tuned via `lists`/`probes` parameters; at very large vector counts (>10M) a dedicated ANN engine (Pinecone, Weaviate) may outperform. Revisit at Phase 13.
- All services share a single Neon project at this phase; a very noisy service could affect query latency for others. Mitigation: per-service connection limits on the pool and Neon's compute autoscaling.
- Schema-per-service inside one database means a catastrophic Postgres failure affects all services simultaneously. Mitigation: Neon managed HA + PITR; in cloud, services can be migrated to separate Neon projects independently.
- PgBouncer transaction-mode pooling forbids session-level `SET` commands — service code must use `SET LOCAL` inside a transaction for RLS context variables (see Contract 13).
