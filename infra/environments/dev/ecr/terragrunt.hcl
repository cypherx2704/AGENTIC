# environments/dev/ecr/terragrunt.hcl — Component 5 (ECR). 13 service repos, identical across envs.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/ecr.hcl"
  expose = true
}

inputs = {}
