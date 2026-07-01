# Module: `iam`

Components **1** (AWS Account & IAM) and **10** (AWS Load Balancer Controller IRSA).

Encodes the platform's IAM separation-of-duty boundary exactly as specified in the
Phase 1 "Note on the IAM separation-of-duty boundary".

## Roles created

| Role | Trust | Permissions | Boundary |
|------|-------|-------------|----------|
| **GitHubActionsRole** | GitHub OIDC (`sts:AssumeRoleWithWebIdentity`) | ECR push (`cypherx/*`), S3 read on Terraform state, `eks:DescribeCluster`, scoped `secretsmanager:GetSecretValue` on `cypherx/ci/*` | **explicit Deny on `iam:*`** — cannot create/modify any IAM resource |
| **TerraformInfraRole** | CI/CD + developers (MFA enforced) | VPC/EC2, EKS, RDS, MSK, ElastiCache, ECR, Route53, ACM, ELB, KMS, S3, DynamoDB; `iam:PassRole` only for the pre-existing node/lbc roles | **Deny on all IAM create/modify verbs** — separation of duty |
| **TerraformIAMRole** | CI/CD + developers (MFA enforced) | IAM only (roles, policies, IRSA mappings, OIDC providers, instance profiles) | **Deny on all infra services** (ec2/eks/rds/kafka/elasticache/route53) |
| **EKSNodeRole** | EC2 service | `AmazonEKSWorkerNodePolicy`, `AmazonEKS_CNI_Policy`, `AmazonEC2ContainerRegistryReadOnly` (ECR **pull** only) | IRSA base for worker nodes |
| **AWSLoadBalancerControllerRole** | EKS cluster OIDC (IRSA) | `ec2:*`, `elasticloadbalancing:*`, `iam:CreateServiceLinkedRole` (+ ACM/WAF/Shield read) | scoped to the `aws-load-balancer-controller` ServiceAccount |

## The self-modification / protected-role guard (do NOT remove)

Both Terraform roles carry an additional inline policy
(`role_self_protection`) that **denies** mutation of:

1. Any role this module manages (GitHubActionsRole, both Terraform roles, the
   node role, the LBC role) — a role cannot grant itself more power or delete a
   peer, and
2. Any role tagged `protected=true` (root/admin roles) — via an
   `aws:ResourceTag/protected = true` condition.

This is the verbatim Component 1 requirement: *"Neither role can modify itself,
the GitHubActionsRole, or any role tagged `protected=true`."*

## Second-approver requirement (CODEOWNERS)

`TerraformIAMRole` is consumed **only** by the `environments/<env>/iam/` Terragrunt
stack. Per Component 1, **PRs that touch that stack require a second human
approver**. Enforce this with a `CODEOWNERS` rule in the repo, e.g.:

```
# .github/CODEOWNERS
/environments/*/iam/      @cypherx-ai/platform-security
/modules/iam/             @cypherx-ai/platform-security
```

Combined with a branch-protection rule requiring CODEOWNERS review, any change to
IAM needs sign-off from the security group in addition to the normal infra
reviewer. The infra Terraform stacks (vpc, eks, rds, …) use `TerraformInfraRole`
and do **not** require this second approver.

## IRSA wiring order

The cluster OIDC provider does not exist until the EKS module has run. Therefore:

1. First apply: `oidc_provider_arn`/`oidc_provider_url` are `null`. The node role
   is created; `AWSLoadBalancerControllerRole` is **skipped** (count = 0).
2. After the EKS module exists, re-apply the IAM stack with the cluster's OIDC
   provider ARN/URL. The LBC IRSA role is then created and its ARN is wired into
   the `k8s-addons/aws-load-balancer-controller` Helm release via the
   `eks.amazonaws.com/role-arn` ServiceAccount annotation.

## No secrets

This module contains **no** secrets. AWS credentials used to apply it come from
the assuming principal (CI OIDC / developer SSO). Doppler/SSM-sourced values are
not referenced here.

## Inputs (highlights)

| Name | Description |
|------|-------------|
| `env`, `account_id`, `region` | environment + account scoping |
| `create_github_oidc_provider` | whether this stack owns the account-global GitHub OIDC provider |
| `github_allowed_repos` | `sub` patterns allowed to assume GitHubActionsRole |
| `terraform_trusted_principal_arns` | principals allowed to assume the Terraform roles (MFA enforced) |
| `state_bucket_arn`, `lock_table_arn` | from `tfstate-backend` |
| `oidc_provider_arn`, `oidc_provider_url` | from `eks` module (enables LBC IRSA) |

## Outputs

`github_actions_role_arn`, `terraform_infra_role_arn`, `terraform_iam_role_arn`,
`eks_node_role_arn`, `aws_lbc_role_arn`, `github_oidc_provider_arn`.
