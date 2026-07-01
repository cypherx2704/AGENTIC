# `postgresql` module — Component 5 (RDS PostgreSQL)

Provisions the platform RDS PostgreSQL 16 instance for an environment, with the exact
parameter group, storage, backup, and encryption settings from Component 5.

Wraps [`terraform-aws-modules/rds/aws`](https://registry.terraform.io/modules/terraform-aws-modules/rds/aws) `~> 6`.

> Spec: `archive/Manoj/phases/phase-01-infrastructure.md` Component 5 (lines 256-270).

## What it creates

| Resource | Detail |
|----------|--------|
| RDS instance | PostgreSQL **16**, `db.r6g.xlarge` (prod) / `db.t3.medium` (dev) |
| Storage | **100 GB gp3**, autoscale to **1 TB**, KMS-encrypted |
| Multi-AZ | **enabled (prod)** / **disabled (dev)** |
| Backups | **7-day** retention, daily window, point-in-time recovery |
| DB subnet group | **private subnets only** |
| Parameter group | the locked-in Component 5 settings (below) |
| KMS key | dedicated key (unless `kms_key_arn` supplied) |
| Performance Insights | enabled, encrypted |

## Parameter group (locked-in — Component 5)

| Parameter | Value | Notes |
|-----------|-------|-------|
| `max_connections` | `1000` | bumped from 500; see connection budget |
| `shared_preload_libraries` | `pg_stat_statements` | **NOT `vector`** |
| `log_min_duration_statement` | `500` | log slow queries >= 500ms |
| `idle_in_transaction_session_timeout` | `60000` | kill idle txns after 60s |

> **pgvector is NOT preloaded.** `pgvector` is installed by the `postgres-bootstrap`
> stack as `CREATE EXTENSION vector` (a regular extension). Do NOT add `vector` to
> `shared_preload_libraries` — only `pg_stat_statements` is preloaded.

> `idle_in_transaction_session_timeout = 60000` works with Contract 13's RLS pattern:
> every tenant-scoped access runs `BEGIN; SET LOCAL app.tenant_id = ...; ...; COMMIT;`.
> Killing idle transactions after 60s prevents an abandoned transaction from leaking
> tenant context across pooled PgBouncer connections.

## Connection budget (RDS <= 1000 conns)

The bump to `max_connections = 1000` covers: PgBouncer runtime pools (~280) + DDL pools
(~70) + direct/operational (~50) + reserve-pool slack (~70) + headroom for the 8th/9th
service (~150) = ~620 used, leaving ~38% headroom. See Component 5 + Component 14.

## Key inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | — | `dev` \| `staging` \| `prod` |
| `instance_class` | `db.r6g.xlarge` | set `db.t3.medium` for dev |
| `multi_az` | `true` | set `false` for dev |
| `allocated_storage_gb` / `max_allocated_storage_gb` | `100` / `1000` | gp3 autoscale |
| `private_subnet_ids` | — | private subnets only |
| `security_group_ids` | — | e.g. `sg-rds` (5432 from `sg-eks-nodes` only) |
| `master_username` / `master_password` | `cypherx_admin` / — | **password from Doppler/SSM**, never hardcoded |
| `database_name` | `cypherx_platform` | initial DB |
| `backup_retention_days` | `7` | |
| `kms_key_arn` | `null` | reuse a key, else one is created |
| `deletion_protection` | `true` | |

## Key outputs

| Name | Notes |
|------|-------|
| `endpoint` | `host:port` — consumed by PgBouncer + service DSNs |
| `address` / `port` | host / port separately |
| `database_name` / `master_username` | |
| `db_subnet_group_name` / `parameter_group_name` | |
| `kms_key_arn` | |

## Secrets

`master_password` is the only secret and is supplied via a Terraform variable sourced from
**Doppler** (`db/admin/master_password`) or SSM. The module sets `manage_master_user_password = false`
so the password comes from the variable, not Secrets Manager — there are **no hardcoded
credentials** in this module. Per-service runtime/DDL users are created later by the
`postgres-bootstrap` stack (Component 16), not here.
