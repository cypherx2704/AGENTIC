# `kafka` module — Component 5 (MSK)

Provisions an Amazon MSK (managed Kafka) cluster with TLS in-transit, KMS at-rest, and
SASL/SCRAM-SHA-512 authentication.

> Spec: `archive/Manoj/phases/phase-01-infrastructure.md` Component 5 (lines 292-301).

## What it creates

| Resource | Detail |
|----------|--------|
| MSK cluster | Kafka **3.6.x**, **3 brokers** (one per AZ) |
| Broker instance | `kafka.m5.large` (prod) / `kafka.t3.small` (dev) |
| Storage | **100 GB gp3** per broker |
| In-transit | **TLS** (`client_broker = TLS`, in-cluster TLS) |
| At-rest | **KMS-encrypted** |
| Auth | **SASL/SCRAM-SHA-512** |
| Configuration | `server.properties`: `auto.create.topics.enable=false`, `min.insync.replicas=2`, `unclean.leader.election.enable=false`, `compression.type=lz4`, `default.replication.factor=3` |
| SCRAM secret | Secrets Manager secret (customer-KMS-encrypted) associated with the cluster |
| Monitoring | enhanced monitoring + open-monitoring (JMX + node exporter) + CloudWatch broker logs |

## Sizing per environment

| | dev | prod |
|--|-----|------|
| `broker_instance_type` | `kafka.t3.small` | `kafka.m5.large` |
| `broker_count` | `3` | `3` |
| `broker_volume_gb` | `100` | `100` |

> dev still runs **3 brokers** (Component 5 / first-cycle checklist: "MSK Kafka (dev: 3 brokers)") —
> only the instance type shrinks.

## Topic ownership (NOT this module)

This module provisions the cluster only. **Topics** (partitions, `cleanup.policy`, retention,
the DLQ pairings, and the compact `auth.agent.*` topics) are owned by the **`kafka-topics`
stack** (Component 17) using the `Mongey/kafka` provider. The cluster sets
`auto.create.topics.enable=false` so topics are declaratively managed, never auto-created.

> **Compact-topic key reminder (Component 17 / Contract 5):** producers to
> `cypherx.auth.agent.registered` and `cypherx.auth.agent.deactivated` MUST set the Kafka
> message key to `agent_id` (NOT `tenant_id`) — a `tenant_id`-keyed compact topic collapses
> to one record per tenant and loses every prior agent state. This is enforced at the
> producer/topics layer, not here, but is called out so it is not lost.

## Key inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `kafka_version` | `3.6.0` | 3.6.x |
| `broker_count` | `3` | multiple of 3 |
| `broker_instance_type` | `kafka.m5.large` | `kafka.t3.small` for dev |
| `broker_volume_gb` | `100` | gp3 |
| `private_subnet_ids` | — | exactly 3 (one per AZ) |
| `security_group_ids` | — | e.g. `sg-kafka` (9092,9094 from `sg-eks-nodes` only) |
| `scram_username` / `scram_password` | `cypherx_app` / — | **password from Doppler**, never hardcoded |
| `kms_key_arn` | `null` | reuse a key, else one is created |

## Key outputs

| Name | Notes |
|------|-------|
| `bootstrap_brokers_sasl_scram` | **primary** client endpoint (SASL/SCRAM over TLS, 9096) |
| `bootstrap_brokers_tls` | TLS-only endpoint (9094) |
| `cluster_arn` / `cluster_name` | cluster identity |
| `scram_secret_arn` | Secrets Manager ARN for the SCRAM credential |
| `kms_key_arn` / `configuration_arn` | |

## Secrets

`scram_password` is the only secret and is supplied via a sensitive Terraform variable
sourced from **Doppler** (`kafka/sasl_password`). It is stored in a Secrets Manager secret
encrypted with a **customer-managed** KMS key (MSK forbids associating the default
`aws/secretsmanager` key). There are **no hardcoded credentials** in this module.
