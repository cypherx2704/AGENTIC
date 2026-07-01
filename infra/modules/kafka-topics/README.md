# `modules/kafka-topics` — Kafka Topic Bootstrap (Component 17)

Declarative, idempotent, **drift-detected** creation of the Contract 5 core topics on the MSK cluster using the
`Mongey/kafka` provider. Component 17 mandates this over `kafka-topics.sh` (which has no plan/apply or drift
reconciliation). A one-shot shell fallback lives at `infra/scripts/kafka-bootstrap-topics.sh`.

Provisioned via `environments/<env>/kafka-topics/terragrunt.hcl`.

## Core topics (exact spec)

| Topic | Partitions | Replication | cleanup.policy | retention |
|-------|-----------|-------------|----------------|-----------|
| `cypherx.auth.agent.registered` | 6 | 3 | compact | infinite (`-1`) |
| `cypherx.auth.agent.deactivated` | 6 | 3 | compact | infinite (`-1`) |
| `cypherx.llms.request.completed` | 12 | 3 | delete | 90 days |
| `cypherx.llms.budget.alert` | 3 | 3 | delete | 30 days |
| `cypherx.guardrails.violation.detected` | 12 | 3 | delete | 90 days |
| `cypherx.agent.task.submitted` | 24 | 3 | delete | 30 days |
| `cypherx.agent.task.completed` | 24 | 3 | delete | 30 days |
| `cypherx.agent.task.failed` | 24 | 3 | delete | 30 days |
| `cypherx.platform.audit.event` | 12 | 3 | delete | 365 days |
| `cypherx.billing.usage.recorded` | 6 | 3 | delete | 365 days |

## Common config (applied to every topic)

```
min.insync.replicas            = 2       # writes require >= 2 ISR acks
unclean.leader.election.enable = false   # no data loss on broker failure
compression.type               = lz4
```

## DLQ topics (Contract 5)

A paired DLQ is created **alongside each non-compact topic**: `cypherx.<original>.dlq`, with the **same partition
count** as the original, replication 3, `cleanup.policy=delete`, retention **30 days**. Example:
`cypherx.agent.task.completed.dlq` → 24 partitions, 30-day retention.

> **Compact topics (`auth.agent.*`) do NOT get a DLQ.** Consumer failure on a compacted topic is recovered by
> re-reading the latest state from the topic on next startup — there is no per-message replay to dead-letter.

## ⚠️ Compact-topic message-key rule (MANDATORY — do not change)

Contract 5 says the envelope `partition_key` **defaults** to `tenant_id`. For the compact `auth.agent.*` topics
that default is **wrong**: log compaction keeps only the latest record per **Kafka message key**, so a
`tenant_id`-keyed compact topic collapses to one record per tenant and loses every prior agent's state.

For `cypherx.auth.agent.registered` and `cypherx.auth.agent.deactivated` (and any future compact topic about an
agent), the **producer MUST set the Kafka message key to `agent_id`** (not `tenant_id`). The envelope
`partition_key` should also be set to `agent_id` for these topics so per-agent ordering is preserved.

This module configures the topics; the message key is set producer-side. This rule is recorded in
`contracts/kafka/topics.md` (Phase 0) and surfaced via the `compact_topics` output.

## Secrets

MSK uses **SASL SCRAM-SHA-512** over TLS (listener port 9096). The admin username/password come from Doppler,
injected as `TF_VAR_kafka_sasl_username` / `TF_VAR_kafka_sasl_password`. Nothing is hardcoded; both variables are
`sensitive`.

```bash
export TF_VAR_kafka_sasl_username="$(doppler secrets get --plain MSK_ADMIN_USER --config dev)"
export TF_VAR_kafka_sasl_password="$(doppler secrets get --plain MSK_ADMIN_PASSWORD --config dev)"
terragrunt apply --terragrunt-working-dir environments/dev/kafka-topics
```

## Inputs (key)

| Variable | Description |
|----------|-------------|
| `bootstrap_servers` | MSK SASL_SSL bootstrap brokers (`host:9096`), from the `kafka` stack via `dependency`. |
| `sasl_username` / `sasl_password` | SCRAM admin creds (Doppler). **Sensitive, required.** |
| `default_replication_factor` | Default `3`. |
| `dlq_retention_ms` | Default 30 days. |

## Outputs

`core_topic_names`, `dlq_topic_names`, `compact_topics`, `all_topic_names`.

## Drift / idempotency

`kafka_topic` resources are declarative. `terragrunt plan` shows config drift (e.g. a manually-changed retention)
and `apply` reconciles it. Changing `partitions` downward is rejected by Kafka — partition counts may only grow;
treat partition changes as deliberate, reviewed events.
