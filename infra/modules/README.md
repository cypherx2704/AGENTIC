# `modules/` — Terraform module index

Cloud-agnostic wrapper modules consumed by the Terragrunt environments under
`environments/<env>/<stack>/`. Every module targets **Terraform >= 1.9** and the
**AWS provider ~> 5.x**, pins its providers in `versions.tf`, and ships
`main.tf` / `variables.tf` / `outputs.tf` / `versions.tf` / `README.md`.

> Spec: `archive/Manoj/phases/phase-01-infrastructure.md`. Component numbers in the
> table point at the authoritative line-ranges.

## Module catalogue

| Module | Component | Purpose | Key inputs | Key outputs |
|--------|-----------|---------|------------|-------------|
| [`vpc`](./vpc) | 3 | VPC `10.0.0.0/16`, 3-AZ private/public subnets, NAT per-AZ, IGW, the six security groups | `env`, `azs`, CIDRs | `vpc_id`, `private_subnet_ids`, `public_subnet_ids`, `sg_*_id` |
| [`tfstate-backend`](./tfstate-backend) | 2 | S3 state bucket + DynamoDB lock table | `account_id` | `state_bucket`, `lock_table` |
| [`iam`](./iam) | 1 | `TerraformInfraRole` / `TerraformIAMRole` split, GitHub OIDC, EKS node role | `account_id`, OIDC config | role ARNs, OIDC provider ARN |
| [`ecr-repo`](./ecr-repo) | 5 | One ECR repo per service (scan-on-push, lifecycle policy) | `repo_names` | repo URLs/ARNs |
| [`eks-cluster`](./eks-cluster) | **4** | EKS 1.30, private-only API, OIDC/IRSA, control-plane logging, managed add-ons, `system-nodes` + `observability` managed NGs | `env`, `vpc_id`, `private_subnet_ids`, `cluster_security_group_ids`, `node_security_group_ids` | `cluster_name`, `cluster_endpoint`, `oidc_provider_arn`, `cluster_security_group_id`, `node_security_group_id` |
| [`postgresql`](./postgresql) | **5** | RDS PostgreSQL 16, multi-AZ, gp3 autoscale, 7-day PITR backups, KMS, Component-5 parameter group | `env`, `instance_class`, `multi_az`, `private_subnet_ids`, `security_group_ids`, `master_password` | `endpoint`, `address`, `port`, `database_name`, `parameter_group_name` |
| [`valkey`](./valkey) | **5** | ElastiCache Valkey 7.x, 3-node prod / 1-node dev, multi-AZ, TLS, AUTH token | `env`, `node_type`, `node_count`, `multi_az_enabled`, `private_subnet_ids`, `security_group_ids`, `auth_token` | `primary_endpoint_address`, `reader_endpoint_address`, `port`, `tls_enabled` |
| [`kafka`](./kafka) | **5** | MSK 3-broker (1/AZ), Kafka 3.6.x, 100GB gp3/broker, TLS in-transit, KMS at-rest, SASL SCRAM-SHA-512 | `env`, `broker_instance_type`, `private_subnet_ids`, `security_group_ids`, `scram_password` | `bootstrap_brokers_sasl_scram`, `bootstrap_brokers_tls`, `cluster_arn`, `scram_secret_arn` |
| [`dns`](./dns) | 5 | Route53 zone records + ACM wildcard certs (per-env + prod aliases) | `env`, zone, hostnames | record FQDNs, cert ARNs |

> Modules in **bold** Component cells (eks-cluster, postgresql, valkey, kafka) are the
> **G2 data-plane** deliverables. Other modules are owned by sibling groups.

## How modules wire together

```
vpc ──┬─ vpc_id, private_subnet_ids ─────────────► eks-cluster, postgresql, valkey, kafka
      ├─ sg_eks_nodes_id ───────────────────────► eks-cluster (node_security_group_ids)
      ├─ sg_rds_id ─────────────────────────────► postgresql  (security_group_ids)
      ├─ sg_valkey_id ──────────────────────────► valkey      (security_group_ids)
      └─ sg_kafka_id ───────────────────────────► kafka       (security_group_ids)

postgresql.endpoint ─► PgBouncer config (Component 14) + service DSNs (Doppler)
valkey.primary_endpoint_address ─► idempotency cache (Contract 9) + service Valkey URLs
kafka.bootstrap_brokers_sasl_scram ─► kafka-topics stack (Component 17) + producers/consumers
eks-cluster.oidc_provider_arn ─► per-service IRSA roles (Phase 2+)
```

## Data-plane sizing matrix (G2 modules)

| Module | dev | prod |
|--------|-----|------|
| `eks-cluster` | system-nodes `t3.medium`x3, observability `m5.large`x2 | same (static; growth is Karpenter NodePools) |
| `postgresql` | `db.t3.medium`, single-AZ | `db.r6g.xlarge`, multi-AZ |
| `valkey` | `cache.t3.micro`, 1 node, single-AZ | `cache.r6g.large`, 3 nodes, multi-AZ |
| `kafka` | `kafka.t3.small`, 3 brokers | `kafka.m5.large`, 3 brokers |

## Conventions honoured across all modules

- **No hardcoded secrets.** DB master password, Valkey AUTH token, and Kafka SCRAM
  password are sensitive variables sourced from **Doppler / SSM**.
- **KMS everywhere.** Each data-plane module creates a dedicated KMS key (rotation on)
  unless an existing `kms_key_arn` is supplied.
- **Private subnets only** for RDS, Valkey, MSK, and EKS nodes/control-plane ENIs.
- **`terraform fmt`-clean** (2-space indent, aligned `=`).
- **Locked-in guards** from the spec are reproduced as comments in the module source and
  the per-module READMEs (e.g. EKS private-only API, managed-NG vs Karpenter non-overlap,
  pgvector-is-not-a-preload, compact-topic `agent_id` key).
