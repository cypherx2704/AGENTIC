# environments/staging/vpc/terragrunt.hcl — Component 3 (VPC). Staging: 3 AZs, NAT per AZ.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/vpc.hcl"
  expose = true
}

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

inputs = {
  az_count               = local.env_vars.locals.az_count
  single_nat_gateway     = local.env_vars.locals.single_nat_gateway
  one_nat_gateway_per_az = !local.env_vars.locals.single_nat_gateway
}
