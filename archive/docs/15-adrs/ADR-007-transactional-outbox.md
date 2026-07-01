# ADR-007 · Transactional Outbox Pattern for Reliable Kafka Event Delivery

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

Every CypherX service needs to emit domain events to Kafka (token-usage metering, audit log, memory lifecycle, task completion signals) at the same time as it writes its domain state to Postgres. The naive approach is to write to Postgres and then publish to Kafka in the same code path. This is a **dual-write**: two separate systems, two separate transaction boundaries. If the Postgres commit succeeds and the Kafka publish fails (network blip, broker unavailable, process crash), the event is silently lost. If the Kafka publish succeeds but the Postgres commit rolls back, a phantom event is emitted for state that never existed. Either failure mode produces a permanent inconsistency that is difficult or impossible to reconcile.

## Decision

All CypherX services adopt the **transactional outbox pattern**. Each service schema includes an `outbox` table (columns: `id UUID`, `event_type TEXT`, `topic TEXT`, `payload JSONB`, `tenant_id UUID`, `trace_id TEXT`, `created_at TIMESTAMPTZ`, `published_at TIMESTAMPTZ NULL`, `attempts INT DEFAULT 0`). When a service writes a domain state change, it also inserts one or more rows into its own `outbox` table **in the same Postgres transaction**. A background relay process (a thread or asyncio task within the same service process) polls the outbox for unpublished rows, publishes each to Kafka, and on broker acknowledgement marks the row `published_at = now()`. Rows that fail after 10 attempts are moved to a dead-letter topic (`cypherx.<domain>.dlq`) and flagged in the outbox for alerting. Published rows are pruned after a configurable retention window (default 72 hours).

The Contract-5 Kafka event envelope (`{ event_id, event_type, schema_version, tenant_id, trace_id, produced_at, payload }`) is assembled from the outbox row at publish time.

## Rationale

### Why This

The outbox pattern resolves the dual-write problem by collapsing two distributed writes into one local write. The outbox row and the domain state row are in the same Postgres transaction: they commit or roll back together. The relay's Kafka publish is then a best-effort at-least-once operation: if the broker is unavailable, the row stays in the outbox and will be retried; if the process crashes mid-publish, the row will be re-fetched on restart and re-published (idempotent consumers handle duplicates). This gives **exactly-once state change + at-least-once event delivery**, which is the strongest guarantee achievable without distributed XA transactions (which have unacceptable performance characteristics).

The relay polling the outbox is simple, observable, and independently testable. Its lag (number of unpublished outbox rows) is a directly metered SLI: an outbox with >100 rows older than 60 seconds triggers an alert.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| Dual-write (write DB then publish Kafka in code) | Fails silently when Kafka is unavailable. If the process crashes between DB commit and Kafka publish, the event is permanently lost. Cannot be made safe without distributed transactions. |
| Kafka Transactions (Kafka's own exactly-once) | Requires that all writes go through Kafka producers with `transactional.id`. Does not help when the domain state must be written to Postgres first. Kafka transactions solve producer-to-consumer exactly-once, not DB-to-Kafka write atomicity. |
| CDC with Debezium / Kafka Connect | Change Data Capture watches the Postgres WAL and emits events for every row change. Powerful and zero-code, but requires deploying Debezium (a separate JVM process), managing Kafka Connect clusters, and granting WAL-read privileges. Operational overhead is high for what is achievable with a simple outbox table + relay. Also, WAL-based CDC emits low-level row changes; the outbox pattern emits domain-shaped events. |
| Saga / two-phase commit | Distributed two-phase commit across Postgres and Kafka is theoretically correct but has 10–100× worse throughput than optimistic single-commit approaches, and no Kafka broker natively participates as an XA resource manager. |
| Event sourcing (Kafka as system of record) | Inverts the model: Kafka is the DB, Postgres is a read model. Appropriate for some domains but fundamentally changes the programming model for all services — too large an architectural shift for Phase 1. |

## Consequences

### Positive

- Atomic coupling of domain state + event: impossible to write state without the event, impossible to emit a phantom event without state.
- At-least-once delivery with observable retry: outbox rows are the audit trail of event publish attempts, visible via SQL.
- DLQ after 10 failures makes poison-pill events observable and debuggable without blocking healthy messages.
- Outbox table is schema-standard across all services — monitoring, tooling, and runbooks are uniform.
- Relay runs in-process alongside the service — no separate deployment unit to manage, no network hop between service and relay.
- `trace_id` propagated from the originating HTTP request into the outbox row and then into the Contract-5 envelope, enabling distributed traces that span DB writes → Kafka publish → consumer processing.

### Negative / Trade-offs

- The relay introduces a publication delay equal to the polling interval (default 500 ms). Events are not published synchronously in the HTTP response path; downstream consumers see a sub-second lag after the state change.
- Each service runs a relay goroutine/thread that must be gracefully shut down on SIGTERM; improper shutdown can leave in-flight publishes in an ambiguous state (resolved by idempotent consumer design).
- Outbox table accumulates rows that must be pruned; a pruning failure causes unbounded table growth. The pruning job must be monitored.
- At-least-once delivery means consumers must be idempotent (deduplication by `event_id`). Services that do not implement idempotent consumers can process duplicate events during relay restarts. Documenting the idempotency requirement is a cross-team discipline requirement.
- The relay's Postgres polling adds read load to the DB. At high event rates (>1 000 events/s), a trigger-based notification (Postgres `LISTEN/NOTIFY` to wake the relay) is more efficient than time-based polling — a future optimization for Phase 13.
