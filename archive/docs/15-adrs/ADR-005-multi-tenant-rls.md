# ADR-005 · PostgreSQL Row-Level Security for Multi-Tenant Data Isolation

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX is a multi-tenant platform where tenant data isolation is a first-class security requirement. Every service stores tenant-scoped records (agent registrations, LLM usage logs, memory entries, RAG knowledge bases, task history). A cross-tenant data leak — tenant A reading tenant B's data — would be a critical security incident. The question is where this isolation is enforced: at the application layer (service code checks `WHERE tenant_id = ?` in every query) or at the database engine layer (Postgres refuses to return rows that do not belong to the active tenant). Application-layer enforcement is fragile: a missing `WHERE` clause, a new developer forgetting the convention, or an ORM that silently drops the predicate can silently expose all tenants' data.

## Decision

All tenant-scoped tables in every service schema use **PostgreSQL Row-Level Security (RLS)**. At the start of every database transaction, the service sets `SET LOCAL app.tenant_id = '<tenant_uuid>'` (using `SET LOCAL` so the variable is scoped to the transaction, not the session — required for PgBouncer transaction-mode pooling). RLS policies on each table evaluate `current_setting('app.tenant_id')` and restrict all operations (SELECT, INSERT, UPDATE, DELETE) to rows where `tenant_id` matches. All services connect as per-schema roles (e.g. `auth_user`, `llms_user`) that are created **without `BYPASSRLS`** — no service code can accidentally or deliberately bypass RLS. The superuser (`postgres`) and the DDL role (`auth_ddl`) are never used by running services.

The `tenant_id` value is extracted from the verified agent JWT (`sub` hierarchy or explicit `tenant_id` claim per Contract 1) and set by the service's request middleware before any query executes. Reserved metadata keys that could spoof `tenant_id` at the application layer are explicitly rejected by xAgent and llms-gateway (Contract 16 anti-spoof guard).

## Rationale

### Why This

Database-enforced isolation is structurally stronger than application-enforced isolation. With RLS active, a service code bug that omits a `tenant_id` filter does not expose other tenants' data — Postgres silently filters to zero rows (or raises a policy violation for INSERT/UPDATE). This converts a potential silent data leak into an observable empty-result bug, which is far safer. The policy is written once per table and applied uniformly to all queries regardless of which code path issued them.

`SET LOCAL` (vs. `SET`) is the correct choice for PgBouncer transaction-mode pooling: `SET LOCAL` scopes the variable to the current transaction and is automatically reset on transaction end, so the next transaction from the pool arrives with no stale `app.tenant_id`. `SET` (session-level) would persist across transaction boundaries in a pooled connection, risking tenant context bleed-through.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Application-layer `WHERE tenant_id = ?` in every query | One missing predicate leaks all tenants' data silently. Enforcing this across a growing codebase via code review is error-prone. Does not protect against SQL injection that bypasses ORM predicates. |
| Separate Postgres database per tenant | Strong isolation but operationally untenable at SaaS scale: N databases means N migration runs, N connection pools, N Neon projects. Makes cross-tenant analytics and platform-level queries impossible. |
| Separate schema per tenant | Better than per-database but still multiplies migration complexity by tenant count. Schema proliferation causes Postgres catalog bloat. Connection pools must be tenant-aware. Rejected in favor of per-service schema with RLS within it. |
| Application-layer middleware that injects tenant filter into every ORM query | Possible with SQLAlchemy events or similar, but relies on the ORM's event system never being bypassed. Raw `cursor.execute()` calls bypass ORM-level hooks. Engine-level enforcement is more robust. |
| Separate service per tenant (microservice isolation) | Extreme operational overhead; not feasible for a multi-tenant SaaS. |

## Consequences

### Positive

- Cross-tenant data access is architecturally impossible from service code: a service running as `llms_user` cannot read another tenant's rows even if it issues a query with no `WHERE` clause.
- Security audits can verify tenant isolation by inspecting RLS policies rather than auditing every query in every service.
- `SET LOCAL` + transaction-mode PgBouncer is compatible with Neon serverless (confirmed in implementation).
- New tables added to a service schema automatically inherit the team's convention of adding an RLS policy as part of the migration — the Atlas migration template enforces this.
- Anti-spoof guards (Contract 16) at the application layer add defense-in-depth: even before the DB layer, malicious `tenant_id` override attempts in request payloads are rejected.

### Negative / Trade-offs

- `SET LOCAL app.tenant_id` must be called at the start of every transaction — a missing call means RLS evaluates `current_setting('app.tenant_id', true)` as NULL, which causes the policy to return zero rows (safe-fail mode by policy design). This can produce confusing empty-result bugs during development.
- PgBouncer transaction mode is mandatory — session mode would allow `SET LOCAL` to be reset between queries in the same session, defeating the isolation. This constraint is documented in Contract 13 and checked in the charts schema validator.
- Platform-level administrative queries (e.g. billing roll-up, cross-tenant analytics) require a separate privileged role with `BYPASSRLS` or a separate analytics pathway — this role must be tightly controlled and never exposed to service code.
- RLS policies add a small overhead to every query plan (policy evaluation is an additional predicate). Benchmarks show <2% overhead on typical indexed queries; acceptable for the isolation guarantee.
- DDL migrations run as the `*_ddl` role which has table-ownership rights but not `BYPASSRLS`; migration scripts that need to seed cross-tenant data must use explicit `tenant_id` values.
