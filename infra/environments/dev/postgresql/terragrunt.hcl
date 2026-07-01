# environments/dev/postgresql/terragrunt.hcl — Component 5 (RDS). Dev: db.t3.medium, single-AZ.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/postgresql.hcl"
  expose = true
}

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

inputs = {
  instance_class = local.env_vars.locals.rds_instance_class # db.t3.medium in dev
  multi_az       = local.env_vars.locals.rds_multi_az       # false in dev
}
