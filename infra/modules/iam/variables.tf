variable "env" {
  description = "Environment name (dev | staging | prod). Used in role names and tags."
  type        = string
}

variable "region" {
  description = "AWS region (e.g. us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "account_id" {
  description = "AWS account ID this stack runs in (cypherx-ai account, Component 1)."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for IAM role names. Roles are named <prefix>-<env>-<role>."
  type        = string
  default     = "cypherx"
}

# ---------------------------------------------------------------------------
# GitHub OIDC trust (GitHubActionsRole — Component 1, Component 18)
# ---------------------------------------------------------------------------

variable "create_github_oidc_provider" {
  description = "Whether this module creates the GitHub OIDC provider. Set false if it already exists in the account (it is account-global, so only one stack should own it)."
  type        = bool
  default     = true
}

variable "github_oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider. Required only when create_github_oidc_provider = false."
  type        = string
  default     = null
}

variable "github_org" {
  description = "GitHub organization that owns the CI repos (the `sub` claim org segment)."
  type        = string
  default     = "cypherx-ai"
}

variable "github_allowed_repos" {
  description = "List of `repo:<org>/<repo>:<ref-or-environment>` subject patterns allowed to assume GitHubActionsRole. Use e.g. repo:cypherx-ai/*:* to allow all repos/refs in the org, or pin per repo/branch."
  type        = list(string)
  default     = ["repo:cypherx-ai/*:*"]
}

# ---------------------------------------------------------------------------
# Trusted principals for the Terraform roles (CI/CD + developers w/ MFA)
# ---------------------------------------------------------------------------

variable "terraform_trusted_principal_arns" {
  description = "IAM principal ARNs (CI role, SSO permission-set roles, break-glass) allowed to assume TerraformInfraRole and TerraformIAMRole. MFA is enforced for these via the trust policy condition."
  type        = list(string)
  default     = []
}

variable "require_mfa_for_terraform" {
  description = "Require aws:MultiFactorAuthPresent = true in the Terraform role trust policies (separation-of-duty hardening for human assumers)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# State backend (GitHubActionsRole + Terraform roles need read/lock on it)
# ---------------------------------------------------------------------------

variable "state_bucket_arn" {
  description = "ARN of the Terraform state S3 bucket (from the tfstate-backend module). GitHubActionsRole gets read; Terraform roles get read/write."
  type        = string
}

variable "lock_table_arn" {
  description = "ARN of the DynamoDB state-lock table. Terraform roles get read/write to acquire locks."
  type        = string
}

# ---------------------------------------------------------------------------
# IRSA (EKS node role + AWS Load Balancer Controller — Components 1, 10)
# ---------------------------------------------------------------------------

variable "oidc_provider_arn" {
  description = "ARN of the EKS cluster OIDC provider (for IRSA trust). Output by the eks module. When null, the AWSLoadBalancerControllerRole IRSA trust is not created (e.g. before the cluster exists)."
  type        = string
  default     = null
}

variable "oidc_provider_url" {
  description = "Issuer URL of the EKS cluster OIDC provider WITHOUT the https:// scheme (e.g. oidc.eks.us-east-1.amazonaws.com/id/ABC123). Required when oidc_provider_arn is set."
  type        = string
  default     = null
}

variable "aws_lbc_namespace" {
  description = "Namespace where the AWS Load Balancer Controller runs."
  type        = string
  default     = "kube-system"
}

variable "aws_lbc_service_account" {
  description = "ServiceAccount name used by the AWS Load Balancer Controller (Component 10)."
  type        = string
  default     = "aws-load-balancer-controller"
}

variable "tags" {
  description = "Tags applied to all IAM resources."
  type        = map(string)
  default     = {}
}
