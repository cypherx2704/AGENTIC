# `environments/` — Terragrunt environment wiring

Terragrunt drives all environment-scoped Terraform. There is **one cluster per environment** (Component 4):
`cypherx-dev`, `cypherx-staging`, `cypherx-prod`, each in its own AWS account / VPC.

## Layout

```
environments/
├── terragrunt.hcl              ← ROOT: S3+DynamoDB remote_state, generated aws provider + versions, common locals
├── _envcommon/                 ← shared per-stack include fragments (env-invariant inputs + module source pointers)
│   ├── vpc.hcl  eks.hcl  kafka.hcl  postgresql.hcl  valkey.hcl  ecr.hcl  dns.hcl  iam.hcl
│   ├── postgres-bootstrap.hcl  kafka-topics.hcl  doppler-bootstrap.hcl
│   └── README.md
├── dev/
│   ├── env.hcl                 ← dev SIZES (single-AZ where the spec allows, small instances)
│   ├── vpc/  eks/  kafka/  postgresql/  valkey/  ecr/  dns/  iam/
│   └── postgres-bootstrap/  kafka-topics/  doppler-bootstrap/
├── staging/                    ← mirrors dev; multi-AZ, mid instances (env.hcl only differs)
└── prod/                       ← mirrors dev; large, multi-AZ; DNS bare aliases enabled (env.hcl only differs)
```

## How sizing works (dev small ↔ prod large)

All env-varying values live in **`<env>/env.hcl`** as `locals`. The `_envcommon/*.hcl` fragments and the thin
`<env>/<stack>/terragrunt.hcl` files read those locals and pass them through as inputs. Adding a new env or
re-sizing is a one-file edit (`env.hcl`); the stack files stay identical across envs.

| Resource | dev | staging | prod |
|----------|-----|---------|------|
| VPC AZs / NAT | 2 AZ, 1 NAT | 3 AZ, NAT/AZ | 3 AZ, NAT/AZ |
| EKS system-nodes | t3.medium ×2 | t3.medium ×3 | t3.medium ×3 (Component 4 fixed) |
| EKS observability | m5.large ×1 | m5.large ×2 | m5.large ×2 (Component 4 fixed) |
| RDS | db.t3.medium, single-AZ | db.r6g.large, multi-AZ | db.r6g.xlarge, multi-AZ |
| Valkey | cache.t3.micro ×1 | cache.r6g.large ×3 | cache.r6g.large ×3, multi-AZ |
| MSK | 3× kafka.t3.small | 3× kafka.m5.large | 3× kafka.m5.large |
| DNS bare aliases | no | no | **yes** (api/auth.cypherx.ai) |

> `core`, `agent`, and `tools` compute are **Karpenter NodePools** (k8s-addons, Component 17b), NOT managed node
> groups. Do not add them here — the two scalers would fight (Component 4 guard).

## Remote state (Component 2)

The root `terragrunt.hcl` configures the S3 backend (`cypherx-terraform-state-<account-id>`, SSE-KMS, versioned)
and the DynamoDB lock table (`cypherx-terraform-locks`). Those resources are provisioned by
`modules/tfstate-backend` (Group G1) and only consumed here. State key is path-scoped:
`cypherx/<env>/<stack>/terraform.tfstate`.

## Apply order (dependencies)

```
iam (TerraformIAMRole stack — 2nd approver gated)
vpc → eks → iam(IRSA) 
vpc → kafka → kafka-topics
vpc → postgresql → postgres-bootstrap
vpc → valkey
ecr, dns                       (no infra deps)
doppler-bootstrap              (one-time human bootstrap on first apply — see module README)
```

Terragrunt resolves these via `dependency` blocks. Use `terragrunt run-all plan` from `environments/<env>/` to
preview the whole env; the graph orders the stacks automatically.

## Secrets

No plaintext secrets in this repo. DB master + runtime + DDL passwords, the Valkey AUTH token, MSK SASL creds, and
the Doppler API token all come from Doppler / AWS Secrets Manager, injected as `TF_VAR_*` at apply time (documented
per stack). RDS master uses the AWS-managed master secret (`manage_master_user_password = true`) so no DB password
ever lands in Terraform state.
