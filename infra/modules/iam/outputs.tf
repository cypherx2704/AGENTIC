output "github_oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider (created or passed through)."
  value       = local.github_oidc_provider_arn
}

output "github_actions_role_arn" {
  description = "ARN of GitHubActionsRole (CI assumes this via OIDC)."
  value       = aws_iam_role.github_actions.arn
}

output "terraform_infra_role_arn" {
  description = "ARN of TerraformInfraRole (default CI role for infra Terraform; cannot touch IAM)."
  value       = aws_iam_role.terraform_infra.arn
}

output "terraform_iam_role_arn" {
  description = "ARN of TerraformIAMRole (IAM-only; used by environments/<env>/iam stack; second-approver via CODEOWNERS)."
  value       = aws_iam_role.terraform_iam.arn
}

output "eks_node_role_arn" {
  description = "ARN of the EKS node role (IRSA base; ECR pull). Passed to the eks module node groups."
  value       = aws_iam_role.eks_node.arn
}

output "eks_node_role_name" {
  description = "Name of the EKS node role."
  value       = aws_iam_role.eks_node.name
}

output "aws_lbc_role_arn" {
  description = "ARN of AWSLoadBalancerControllerRole (IRSA). Null until the EKS OIDC provider is supplied."
  value       = local.irsa_enabled ? aws_iam_role.aws_lbc[0].arn : null
}
