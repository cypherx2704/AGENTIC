# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/iam.hcl — shared inputs for the IAM stack (Component 1).
# This is the SEPARATE, smaller IAM stack: TerraformIAMRole territory. PRs against it require a second human
# approver in CI (CODEOWNERS). It provisions IRSA roles + the role split; it does NOT provision infra.
# Roles: GitHubActionsRole, TerraformInfraRole, TerraformIAMRole, EKS node role, IRSA service-account roles.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//iam"
}

dependency "eks" {
  config_path = "../eks"

  mock_outputs = {
    cluster_name      = "cypherx-mock"
    oidc_provider_arn = "arn:aws:iam::000000000000:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/MOCK"
    oidc_provider_url = "oidc.eks.us-east-1.amazonaws.com/id/MOCK"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  env = local.env

  cluster_name      = dependency.eks.outputs.cluster_name
  oidc_provider_arn = dependency.eks.outputs.oidc_provider_arn
  oidc_provider_url = dependency.eks.outputs.oidc_provider_url

  # Component 1: GitHub OIDC trust for GitHubActionsRole. Repo allow-list comes from env.hcl.
  github_oidc_subjects = local.env_vars.locals.github_oidc_subjects

  # Component 1 separation-of-duty:
  #   TerraformInfraRole — VPC/EKS/RDS/MSK/ElastiCache/ECR/Route53; CANNOT touch IAM.
  #   TerraformIAMRole   — IAM roles + IRSA mappings only; second-approver gated.
  # Neither role may modify itself, GitHubActionsRole, or any role tagged protected=true.
  manage_role_split = true

  # Component 10: AWS LBC IRSA role (ec2:*, elasticloadbalancing:*, iam:CreateServiceLinkedRole).
  create_aws_lbc_irsa = true
}
