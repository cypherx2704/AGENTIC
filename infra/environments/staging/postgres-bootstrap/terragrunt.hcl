# environments/staging/postgres-bootstrap/terragrunt.hcl — Component 16. Runs once per env.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/postgres-bootstrap.hcl"
  expose = true
}

inputs = {}
