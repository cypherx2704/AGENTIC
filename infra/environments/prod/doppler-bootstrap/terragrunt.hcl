# environments/prod/doppler-bootstrap/terragrunt.hcl — Components 11 + 20. One-time human bootstrap per env.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/doppler-bootstrap.hcl"
  expose = true
}

inputs = {}
