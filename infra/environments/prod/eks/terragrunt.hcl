# environments/prod/eks/terragrunt.hcl — Component 4 (EKS). cypherx-prod cluster. system 3 / observability 2 fixed.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/eks.hcl"
  expose = true
}

inputs = {}
