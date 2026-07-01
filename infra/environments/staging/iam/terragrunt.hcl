# environments/staging/iam/terragrunt.hcl — Component 1 (IAM). SEPARATE stack; second-approver gated in CI.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/iam.hcl"
  expose = true
}

inputs = {}
