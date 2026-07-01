# CypherX Infrastructure (`cypherx-infra`)

Phase 1 — Infrastructure Foundation. Terraform + Terragrunt IaC plus
Kubernetes add-ons that produce a running, secure, observable EKS platform per
environment (`dev`, `staging`, `prod`).

> Authoritative spec: `archive/Manoj/phases/phase-01-infrastructure.md`.
> Contracts cross-referenced: `archive/Manoj/phases/phase-00-contracts.md`.

## Repository layout

```
infra/
├── modules/                      ← cloud-agnostic wrapper modules (raw + official)
│   ├── tfstate-backend/          ← S3 + DynamoDB remote-state backend        (Component 2)
│   ├── iam/                      ← role split + IRSA + GitHub OIDC          (Components 1, 10)
│   ├── vpc/                      ← VPC, subnets, NAT, IGW, 6 security groups (Component 3)
│   ├── ecr-repo/                 ← reusable ECR repo (scan, lifecycle)       (Component 5)
│   ├── dns/                      ← Route53 zone, per-env ACM, records        (Component 5)
│   ├── s3-bucket/                ← reusable encrypted bucket (loki/tempo)     (Component 13)
│   ├── eks/                      ← EKS cluster + node groups                 (Component 4)   [other group]
│   ├── postgresql/               ← RDS PostgreSQL                            (Component 5)   [other group]
│   ├── valkey/                   ← ElastiCache Valkey                        (Component 5)   [other group]
│   └── kafka/                    ← MSK                                       (Component 5)   [other group]
│
├── environments/                 ← Terragrunt: per-env stacks
│   ├── terragrunt.hcl            ← root: remote_state (S3+DynamoDB) + provider generate
│   ├── dev/      (env.hcl + per-stack terragrunt.hcl — small, single-AZ)
│   ├── staging/  (env.hcl + per-stack terragrunt.hcl)
│   └── prod/     (env.hcl + per-stack terragrunt.hcl — large, multi-AZ)
│       stacks: vpc, eks, kafka, postgresql, valkey, ecr, dns, iam,
│               postgres-bootstrap, kafka-topics, doppler-bootstrap
│
├── k8s-addons/                   ← Terraform-managed Helm releases            (Components 7-13, 17b)
│   ├── istio/  kong/  argocd/  cert-manager/  aws-load-balancer-controller/
│   ├── prometheus-stack/  loki/  tempo/  doppler-operator/
│   └── metrics-server/  karpenter/  external-dns/  reloader/
│
├── dev-local/                    ← Tilt + kind + docker-compose local story   (Component 17c)
├── scripts/                      ← bootstrap + smoke-test scripts             (Components 17, 21)
└── smoketest/                    ← echo-service + infra smoke test            (Component 21)
```

> This group (G1 — TF foundation) authored: `modules/tfstate-backend`,
> `modules/iam`, `modules/vpc`, `modules/ecr-repo`, `modules/dns`,
> `modules/s3-bucket`, and this root README. The `eks`, `postgresql`, `valkey`,
> `kafka` modules, the `environments/`, `k8s-addons/`, `dev-local/`, `scripts/`,
> and `smoketest/` trees are authored by sibling groups.

## Tooling baseline

| Tool | Version |
|------|---------|
| Terraform | `>= 1.9` |
| AWS provider | `~> 5.0` (pinned per module in `versions.tf`) |
| Terragrunt | drives `environments/` (remote state + provider generation) |
| Helm | 3 (k8s-addons + charts) |

Every module directory contains `main.tf`, `variables.tf`, `outputs.tf`,
`versions.tf`, and `README.md`. Code is `terraform fmt`-clean (2-space indent,
aligned `=`). **No secrets are hardcoded** — DB/auth passwords and Doppler/AWS
credentials are sourced from Doppler/SSM and passed as variables.

## The IAM role split (Components 1 & 10)

Separation of duty is enforced at the IAM layer (`modules/iam`):

| Role | Used by | Can | Cannot |
|------|---------|-----|--------|
| **GitHubActionsRole** | GitHub Actions (OIDC) | ECR push, S3 state **read**, `eks:DescribeCluster`, scoped `cypherx/ci/*` secrets | **any IAM action** (explicit deny) |
| **TerraformInfraRole** | CI + devs (MFA) for all infra stacks | VPC/EKS/RDS/MSK/ElastiCache/ECR/Route53/ACM/KMS/S3/DynamoDB; `PassRole` of node/lbc roles only | **create/modify IAM** |
| **TerraformIAMRole** | the `environments/<env>/iam/` stack only | IAM roles, policies, IRSA, OIDC providers | **all infra services** |
| **EKSNodeRole** | EC2 worker nodes | worker baseline + ECR **pull** | — |
| **AWSLoadBalancerControllerRole** | LBC ServiceAccount (IRSA) | `ec2:*`, `elasticloadbalancing:*`, `iam:CreateServiceLinkedRole` | — |

**Guard (do not remove):** neither Terraform role can modify itself,
GitHubActionsRole, or any role tagged `protected=true`.

**Second approver:** changes under `environments/*/iam/` and `modules/iam/`
require a second human reviewer via `CODEOWNERS` (see `modules/iam/README.md`).
Infra stacks use `TerraformInfraRole` and do not need this.

## Bootstrap order (per AWS account / per env)

1. **State backend** — apply `modules/tfstate-backend` with a local backend,
   then migrate state into the bucket it creates (`modules/tfstate-backend/README.md`).
2. **IAM** — apply the IAM stack (`TerraformIAMRole`). Creates GitHubActionsRole,
   the two Terraform roles, the EKS node role, GitHub OIDC provider. (LBC IRSA
   role is created on a later re-apply once the EKS OIDC provider exists.)
3. **Networking** — `vpc` (VPC, subnets, NAT, the 6 SGs).
4. **DNS** — `dns` (hosted zone once at the account level, per-env ACM wildcard).
5. **Compute + data** — `eks`, then `ecr`, `postgresql`, `valkey`, `kafka`
   (other group's modules).
6. **Re-apply IAM** with the EKS OIDC provider ARN/URL → creates
   `AWSLoadBalancerControllerRole` (IRSA).
7. **k8s-addons**, **bootstrap** stacks (postgres-bootstrap, kafka-topics,
   doppler-bootstrap), then the **Component 21 smoke test** (must pass twice).

## Applying per environment with Terragrunt

The root `environments/terragrunt.hcl` configures the S3 + DynamoDB backend
(from step 1) and generates the AWS provider block. Each `env.hcl` carries the
env-varying sizes (`dev` = small/single-AZ, `prod` = large/multi-AZ).

```bash
# plan/apply a single stack in one env
cd environments/dev/vpc
terragrunt plan
terragrunt apply

# apply every stack in an env (dependency order resolved by terragrunt)
cd environments/dev
terragrunt run-all apply
```

CI assumes `TerraformInfraRole` for everything except the `iam/` stack, which
uses `TerraformIAMRole` and requires the CODEOWNERS second approver.

## Environment sizing summary

| Resource | dev | prod |
|----------|-----|------|
| NAT gateways | 1 (shared) | 3 (1/AZ, HA) |
| RDS | `db.t3.medium`, single-AZ | `db.r6g.xlarge`, multi-AZ |
| Valkey | 1 node `cache.t3.micro` | 3 nodes `cache.r6g.large`, multi-AZ |
| Kafka | `kafka.t3.small` × 3 | `kafka.m5.large` × 3 |
| ACM | `*.dev.cypherx.ai` | `*.prod.cypherx.ai` + `cypherx.ai`/`*.cypherx.ai` + bare aliases |

## Key contract guards encoded in this repo (do NOT "fix")

- **ALB→Kong is plaintext HTTP** inside the VPC (`sg-alb`→`sg-kong` on 8000);
  Kong→backend is mTLS via Istio (`modules/vpc` + Component 8).
- **`/v1/agents/*` routes to Auth, not xAgent** (Kong config, Component 8).
- **Managed node groups vs Karpenter are non-overlapping** (`system-nodes`,
  `observability` are managed; `core`/`agent`/`tools` are Karpenter — Component 4).
- **Compact `auth.agent.*` Kafka topics use `agent_id` as the message key**, not
  `tenant_id` (Component 17 / Contract 5).
- **`CREATEROLE` is cluster-wide, not schema-scoped** — mitigated, not solved
  (Component 16 / Contract 14).
- **`iss` (stable) vs JWKS URL (per-env) split** — verifiers treat `iss` as
  opaque, discover JWKS per-env (`modules/dns` + Contract 1).

## Security posture

- No `0.0.0.0/0` ingress on any private data service (`sg-rds`/`sg-valkey`/`sg-kafka`).
- State bucket: versioned, SSE-KMS, public-access-blocked, TLS-enforced,
  noncurrent versions expire after 90 days.
- All passwords/tokens come from Doppler/SSM at apply/runtime — never committed.
