# `valkey` module — Component 5 (ElastiCache Valkey)

Provisions an ElastiCache **Valkey 7.x** replication group with TLS in-transit, an AUTH
token, and KMS at-rest encryption.

> Spec: `archive/Manoj/phases/phase-01-infrastructure.md` Component 5 (lines 282-290).

## What it creates

| Resource | Detail |
|----------|--------|
| Replication group | Valkey **7.x**, `cache.r6g.large` (prod) / `cache.t3.micro` (dev) |
| Nodes | **3 (prod)** / **1 (dev)** |
| Multi-AZ | **enabled (prod)** — requires >= 2 nodes (auto-failover) |
| TLS | **enabled** (`transit_encryption_enabled = true`) |
| AUTH | **AUTH token** (from Doppler) |
| At-rest | **KMS-encrypted** |
| Subnet group | private subnets |
| Parameter group | `valkey7` family |
| KMS key | dedicated key (unless `kms_key_arn` supplied) |

## Sizing per environment

| | dev | prod |
|--|-----|------|
| `node_count` | `1` | `3` |
| `node_type` | `cache.t3.micro` | `cache.r6g.large` |
| `multi_az_enabled` | `false` (single node) | `true` |

Multi-AZ + automatic failover require >= 2 nodes; with `node_count = 1` the module disables
both automatically. `cache.t3.micro` does not support snapshots, so retention is forced to
`0` there.

## Key inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `engine_version` | `7.2` | Valkey 7.x |
| `node_type` | `cache.r6g.large` | `cache.t3.micro` for dev |
| `node_count` | `3` | `1` for dev |
| `multi_az_enabled` | `true` | `false` for dev |
| `private_subnet_ids` | — | cache subnet group |
| `security_group_ids` | — | e.g. `sg-valkey` (6379 from `sg-eks-nodes` only) |
| `auth_token` | — | **from Doppler**, 16-128 chars, never hardcoded |
| `kms_key_arn` | `null` | reuse a key, else one is created |

## Key outputs

| Name | Notes |
|------|-------|
| `primary_endpoint_address` | writes (multi-node) |
| `reader_endpoint_address` | reads (multi-node) |
| `configuration_endpoint_address` | cluster-mode only |
| `port` | `6379` |
| `tls_enabled` | always `true` |
| `kms_key_arn` | |

## Secrets

`auth_token` is the only secret and is supplied via a sensitive Terraform variable sourced
from **Doppler** (the Component 5 AUTH token). It is **never hardcoded** and is set to
`ignore_changes` so out-of-band rotation does not force replacement on every plan.
