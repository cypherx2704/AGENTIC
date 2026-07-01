# Kafka Topic Registry — Contract 5

> **Normative reference for Contract 5 (Kafka Event Envelope & Topic Registry).**
> Every Kafka event produced by any CypherX service MUST use the
> [event envelope](./event-envelope.schema.json). The `payload` field is event-type-specific
> and is validated against the matching schema under [`events/`](./events/).

---

## 1. Topic naming convention

```
cypherx.<domain>.<entity>.<event-type>
```

Examples:

```
cypherx.auth.agent.registered
cypherx.llms.request.completed
cypherx.agent.task.failed
cypherx.guardrails.violation.detected
```

The envelope `event_type` field MUST equal the fully-qualified event name and mirror the
topic it is published to.

---

## 2. Foreign-prefix allow-list

External systems publishing into our Kafka are restricted to an explicit allow-list of
non-`cypherx.` prefixes:

| Prefix | Owner | Purpose |
|--------|-------|---------|
| `px0.*` | px0 platform | Org lifecycle, billing. See Contract 13. |

- Any other non-`cypherx.` prefix is **forbidden** without an explicit
  contract-changelog entry.
- The `px0.*` foreign-prefix topics are consumed by the **px0-bridge** service **only**.
  All other services subscribe exclusively to `cypherx.tenant.*` (Contract 13) — never to
  `px0.*` directly. Source-specific adapters (px0-bridge, billing-bridge, sso-jit-handler)
  translate external events into CypherX-native `cypherx.*` topics.

---

## 3. Dead-letter topics (DLQ)

- Every consumer MUST have a paired DLQ topic named `<original-topic>.dlq`.
- DLQ messages MUST **wrap the original envelope** and add a `dlq_metadata` object:

```json
{
  "dlq_metadata": {
    "failed_at": "2026-05-22T10:00:00.000Z",
    "consumer_service": "billing-aggregator",
    "error_code": "DESERIALIZE_FAILED",
    "error_message": "payload did not match schema_version 1.0.0",
    "retry_count": 3
  }
}
```

| `dlq_metadata` field | Meaning |
|----------------------|---------|
| `failed_at` | RFC 3339 UTC timestamp (ms precision) when the consumer gave up. |
| `consumer_service` | Logical name of the consumer that failed to process the message. |
| `error_code` | Machine-readable failure code. |
| `error_message` | Human-readable failure description. |
| `retry_count` | Number of delivery attempts before routing to the DLQ. |

Example: `cypherx.agent.task.completed` → DLQ `cypherx.agent.task.completed.dlq`.

---

## 4. Partition key & ordering

- `partition_key` MUST be present on every event.
- `partition_key` MUST **default to `tenant_id`** for tenant-scoped events. This guarantees
  per-tenant ordering.
- Event types that need stronger ordering (e.g. per-agent) MAY override `partition_key` with
  `agent_id`, but MUST document the override in this file.

### 4.1 MANDATORY compact-topic message-key rule

> **Compact topics MUST be keyed by `agent_id`, NOT `tenant_id` (Phase 1 Component 17).**

Log compaction keeps **only the latest record per Kafka message key**. A `tenant_id`-keyed
compact topic therefore collapses to **one record per tenant** and silently discards every
prior agent's state — all agents in a tenant overwrite each other down to a single survivor.

For the following compact topics (and **any future compact topic about an agent**), the
producer MUST set **both**:

- the **Kafka message key** to `agent_id` (not `tenant_id`), so compaction retains the latest
  state per agent; and
- the envelope **`partition_key`** to `agent_id`, so per-agent ordering is preserved.

| Compact topic | Kafka message key | Envelope `partition_key` |
|---------------|-------------------|--------------------------|
| `cypherx.auth.agent.registered` | `agent_id` | `agent_id` |
| `cypherx.auth.agent.deactivated` | `agent_id` | `agent_id` |

This overrides the Contract 5 `partition_key` default of `tenant_id` for these topics. Producers
on compact topics MUST NOT fall back to the `tenant_id` default.

**Documented partition-key overrides:**

| Topic | `partition_key` | Reason |
|-------|-----------------|--------|
| `cypherx.auth.agent.registered` | `agent_id` | Compact topic — `tenant_id` would collapse all agents to one record (see §4.1). |
| `cypherx.auth.agent.deactivated` | `agent_id` | Compact topic — `tenant_id` would collapse all agents to one record (see §4.1). |

All other first-cycle topics use `partition_key = tenant_id`.

---

## 5. First-cycle topic registry

Every first-cycle topic, its producer, and the payload schema that validates the envelope
`payload`. Payload schemas live under [`events/`](./events/) (or
[`usage/usage-event.schema.json`](../usage/usage-event.schema.json) for usage metering).

### 5.1 Task / LLMs / Guardrails / Auth topics

| Topic | Producer | Payload schema |
|-------|----------|----------------|
| `cypherx.auth.agent.registered` | Auth | [`events/auth.agent.registered.schema.json`](./events/auth.agent.registered.schema.json) |
| `cypherx.llms.request.completed` | LLMs gateway | [`events/llms.request.completed.schema.json`](./events/llms.request.completed.schema.json) |
| `cypherx.guardrails.violation.detected` | Guardrails | [`events/guardrails.violation.detected.schema.json`](./events/guardrails.violation.detected.schema.json) |
| `cypherx.agent.task.completed` | xAgent | [`events/agent.task.completed.schema.json`](./events/agent.task.completed.schema.json) |
| `cypherx.agent.task.failed` | xAgent | [`events/agent.task.failed.schema.json`](./events/agent.task.failed.schema.json) |

> These five payload schemas MUST be checked into the repo before Phase 1 starts. They are
> first-cycle and back the Contract 15 smoke test.

### 5.2 Tenant lifecycle topics (Contract 13)

Emitted by Auth regardless of upstream `source`. All services subscribe only to
`cypherx.tenant.*`.

| Topic | Producer | Triggered by | Payload schema |
|-------|----------|--------------|----------------|
| `cypherx.tenant.created` | Auth | Any tenant source (px0-bridge, external-admin, self-serve-signup, sso-jit, manual-seed) | [`events/tenant.created.schema.json`](./events/tenant.created.schema.json) |
| `cypherx.tenant.suspended` | Auth | px0 `org.suspended` OR billing failure OR admin action OR self-serve cancellation | [`events/tenant.suspended.schema.json`](./events/tenant.suspended.schema.json) |
| `cypherx.tenant.plan_changed` | Auth | Billing event from any billing adapter (Contract 19 emitter, e.g. px0 / Stripe / Chargebee) | [`events/tenant.plan_changed.schema.json`](./events/tenant.plan_changed.schema.json) |
| `cypherx.tenant.deleted` | Auth | px0 `org.deleted` OR admin action OR self-serve close-account + 30-day grace | [`events/tenant.deleted.schema.json`](./events/tenant.deleted.schema.json) |

### 5.3 Per-service usage-metering topics (Contract 19.1)

Every SharedCore service emits one metering event per billable operation on its
service-specific usage topic. All usage-metering topics share the common usage-event payload
shape: [`usage/usage-event.schema.json`](../usage/usage-event.schema.json).

| Service | Topic | Payload schema |
|---------|-------|----------------|
| Auth | `cypherx.auth.usage.recorded` | [`usage/usage-event.schema.json`](../usage/usage-event.schema.json) |
| LLMs | `cypherx.llms.usage.recorded` (alias of `cypherx.llms.request.completed`) | [`usage/usage-event.schema.json`](../usage/usage-event.schema.json) |
| Guardrails | `cypherx.guardrails.usage.recorded` | [`usage/usage-event.schema.json`](../usage/usage-event.schema.json) |
| RAG | `cypherx.rag.usage.recorded` | [`usage/usage-event.schema.json`](../usage/usage-event.schema.json) |
| Memory | `cypherx.memory.usage.recorded` | [`events/memory.usage.recorded.schema.json`](./events/memory.usage.recorded.schema.json) (specialises [`usage/usage-event.schema.json`](../usage/usage-event.schema.json)) |

> The LLMs usage topic is an alias of `cypherx.llms.request.completed` — the request-completed
> event already carries `prompt_tokens`, `completion_tokens`, and `cost_usd`.

> The Memory usage topic uses a dedicated payload schema
> [`events/memory.usage.recorded.schema.json`](./events/memory.usage.recorded.schema.json) that is a
> member of the usage-event family — it keeps the common `tenant_id`/`operation`/`units` shape from
> [`usage/usage-event.schema.json`](../usage/usage-event.schema.json) and is forward-compatible with it
> (additive specialisation: Memory-specific `operation`/`units` guidance, e.g. `write`/`recall`/`score`).
> `partition_key` = `tenant_id` (default; non-compact, so it also gets a paired
> `cypherx.memory.usage.recorded.dlq`).

### 5.4 Phase 1 operational topics (Component 17)

These topics are created on MSK cluster bootstrap by the declarative Kafka topic stack
(`environments/<env>/kafka-topics/`, Phase 1 Component 17). They are listed here with their
broker-level provisioning config so the registry is the single source of truth for partitions,
replication, cleanup policy, retention, and DLQ pairing. All topics also carry the common
broker config `min.insync.replicas = 2`, `unclean.leader.election.enable = false`,
`compression.type = lz4`.

| Topic | Producer | Partitions | Replication | `cleanup.policy` | Retention | DLQ |
|-------|----------|------------|-------------|------------------|-----------|-----|
| `cypherx.auth.agent.deactivated` | Auth | 6 | 3 | `compact` | infinite | none (compact — re-read latest state) |
| `cypherx.llms.budget.alert` | LLMs gateway | 3 | 3 | `delete` | 30 days | `cypherx.llms.budget.alert.dlq` (3 partitions, 30d) |
| `cypherx.agent.task.submitted` | xAgent | 24 | 3 | `delete` | 30 days | `cypherx.agent.task.submitted.dlq` (24 partitions, 30d) |
| `cypherx.platform.audit.event` | Platform mgmt | 12 | 3 | `delete` | 365 days | `cypherx.platform.audit.event.dlq` (12 partitions, 30d) |
| `cypherx.billing.usage.recorded` | Platform mgmt / billing | 6 | 3 | `delete` | 365 days | `cypherx.billing.usage.recorded.dlq` (6 partitions, 30d) |

> **DLQ rule (Contract 5 §3, Component 17):** every non-compact topic gets a paired
> `<original>.dlq` with the **same partition count**, replication 3, `cleanup.policy = delete`,
> and **30-day retention** (regardless of the source topic's retention). Compact topics
> (`cypherx.auth.agent.deactivated`, `cypherx.auth.agent.registered`) do **NOT** get a DLQ —
> a failed consumer re-reads the latest compacted state on next startup.

> `cypherx.auth.agent.deactivated` is a compact topic about an agent and is therefore subject to
> the §4.1 message-key rule: Kafka message key and envelope `partition_key` MUST be `agent_id`.

---

## 6. Schema cross-reference

Every payload schema referenced by this registry:

- [`events/auth.agent.registered.schema.json`](./events/auth.agent.registered.schema.json)
- [`events/llms.request.completed.schema.json`](./events/llms.request.completed.schema.json)
- [`events/guardrails.violation.detected.schema.json`](./events/guardrails.violation.detected.schema.json)
- [`events/agent.task.completed.schema.json`](./events/agent.task.completed.schema.json)
- [`events/agent.task.failed.schema.json`](./events/agent.task.failed.schema.json)
- [`events/tenant.created.schema.json`](./events/tenant.created.schema.json)
- [`events/tenant.suspended.schema.json`](./events/tenant.suspended.schema.json)
- [`events/tenant.plan_changed.schema.json`](./events/tenant.plan_changed.schema.json)
- [`events/tenant.deleted.schema.json`](./events/tenant.deleted.schema.json)
- [`events/memory.usage.recorded.schema.json`](./events/memory.usage.recorded.schema.json) (Memory usage metering — usage-event family)
- [`usage/usage-event.schema.json`](../usage/usage-event.schema.json) (usage metering)

The envelope itself: [`event-envelope.schema.json`](./event-envelope.schema.json).
