locals {
  prefix = "${var.name_prefix}-${var.env}"

  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.github_oidc_provider_arn

  # IRSA is only wired when the EKS OIDC provider ARN is supplied (post-cluster).
  irsa_enabled = var.oidc_provider_arn != null && var.oidc_provider_url != null

  # ARNs of roles this module manages — used to build the self-modification /
  # protected-role deny boundary referenced by every Terraform role policy.
  managed_role_arns = [
    "arn:aws:iam::${var.account_id}:role/${local.prefix}-github-actions",
    "arn:aws:iam::${var.account_id}:role/${local.prefix}-terraform-infra",
    "arn:aws:iam::${var.account_id}:role/${local.prefix}-terraform-iam",
    "arn:aws:iam::${var.account_id}:role/${local.prefix}-eks-node",
    "arn:aws:iam::${var.account_id}:role/${local.prefix}-aws-lbc",
  ]

  common_tags = merge(var.tags, {
    Environment = var.env
    ManagedBy   = "terraform"
    Module      = "iam"
  })
}
