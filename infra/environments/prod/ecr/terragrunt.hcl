# environments/prod/ecr/terragrunt.hcl — Component 5 (ECR). 13 service repos.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/ecr.hcl"
  expose = true
}

inputs = {}
