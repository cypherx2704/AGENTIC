# environments/dev/iam/terragrunt.hcl — Component 1 (IAM). SEPARATE stack: TerraformIAMRole territory.
# PRs against this stack require a second human approver in CI (CODEOWNERS rule).
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/iam.hcl"
  expose = true
}

inputs = {}
