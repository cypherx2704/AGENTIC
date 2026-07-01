# ADR-002 · Kafka for Event Streaming with Transactional Outbox

**Status:** Accepted  
**Date:** 2026-06-01  
**Deciders:** CypherX Platform Team

## Context

CypherX services emit domain events that drive downstream consumers: token-usage billing aggregation, audit trails, memory lifecycle GDPR-wipe triggers, and future analytics pipelines. These events must be delivered reliably — a token-usage event that is lost means a billing gap; an audit event that is dropped means a compliance hole. Services write to both a database and an event broker, creating the classic dual-write problem: if the DB commit succeeds but the broker publish fails (or vice versa), the system is inconsistent. Additionally, the local development stack must be Kafka-compatible without requiring a full managed Kafka cluster.

## Decision

Apache Kafka is the event streaming backbone for all CypherX cross-service eventing. In the local compose stack, **Redpanda** (Kafka-compatible, single binary) replaces a full Kafka cluster. In the cloud, AWS MSK (managed Kafka) is the target. All services adopt the **transactional outbox pattern**: domain events are written to a local `<schema>.outbox` table in the same database transaction as the state change, then a relay process reads and publishes the events to Kafka, deleting or marking them after successful acknowledgement. The Contract-5 envelope (`{ topic, event_type, tenant_id, trace_id, payload, schema_version }`) is mandatory for every event. Topic naming convention: `cypherx.<domain>.<entity>.<event-type>` (e.g. `cypherx.agent.task.completed`). Each consumer group maintains a dead-letter queue (DLQ) topic after 10 failed delivery attempts.

## Rationale

### Why This

Kafka provides durable, ordered, replayable event logs with at-least-once delivery semantics, which are appropriate for billing and audit workloads where missed events are more dangerous than duplicate events (duplicates can be deduplicated; missing events cannot be recovered without source replay). The transactional outbox pattern eliminates the dual-write problem entirely: by writing the event to the same Postgres transaction as the domain state change, the event is guaranteed to exist in the DB if and only if the state change committed. The relay's at-least-once publish to Kafka then only needs to handle idempotent consumers, which is a simpler invariant to enforce than distributed two-phase commit.

Redpanda's Kafka API compatibility means all service producer/consumer code (Python `aiokafka`, Kotlin `kafka-clients`) runs unchanged against both local Redpanda and cloud MSK. This eliminates the "works locally, fails in CI" class of event bugs.

### Alternatives Considered

| Option | Why Rejected |
|--------|-------------|
| AWS SQS / SNS | No log replay; messages deleted after consumption; no consumer group offset management; fan-out requires explicit SNS topic per event type; no Kafka-compatible local equivalent for dev. |
| RabbitMQ | Broker-centric routing (exchanges/queues) is flexible but messages are not replayable after acknowledgement; no native Kafka compatibility; dead-lettering is per-queue not per-consumer-group. |
| Google Pub/Sub | GCP-native; no self-hosted local equivalent; no Kafka-compatible client; billing model by message volume is harder to predict at scale. |
| Direct HTTP webhooks between services | No durability; caller must retry on receiver unavailability; tight coupling; no fan-out without explicit N calls; loses event ordering guarantees. |
| Postgres LISTEN/NOTIFY | Zero infrastructure, but limited payload size (8 KB), no consumer group semantics, no persistent log replay, not suitable for cross-service fan-out or high-throughput metering. |

## Consequences

### Positive

- Dual-write problem is solved structurally: outbox-in-same-transaction means event existence is tied to state change atomicity.
- At-least-once delivery with DLQ gives observable failure modes — a stuck DLQ is an alert, not silent data loss.
- Replayable log enables future analytics, audit replay, and backfill of new consumers from historical events.
- Redpanda in compose is a single container with no ZooKeeper dependency; startup time in local dev is under 5 seconds.
- Contract-5 envelope carries `trace_id` and `tenant_id`, so every downstream consumer can participate in distributed tracing and maintain tenant isolation without parsing event payloads.
- Topic naming convention `cypherx.<domain>.<entity>.<event-type>` makes Kafka ACL policies expressible as prefix patterns.

### Negative / Trade-offs

- Redpanda in compose adds ~300 MB to the compose stack memory footprint.
- At-least-once semantics require all consumers to be idempotent (deduplication by `event_id`). Services that are not yet idempotent can receive duplicate events during relay retries.
- The outbox relay is a background thread/process per service — it must be monitored and its lag alerted on; an unhealthy relay silently accumulates outbox rows without failing the main service.
- MSK in cloud requires VPC configuration, IAM authentication, and a TLS-only policy — this is additional infrastructure complexity vs. a managed SaaS like Confluent Cloud (but avoids per-message cost at scale).
- Schema evolution of event payloads requires backward-compatible changes or a schema registry (Redpanda ships one on port 8081); consumers must handle unknown fields gracefully per forward-compat convention.
