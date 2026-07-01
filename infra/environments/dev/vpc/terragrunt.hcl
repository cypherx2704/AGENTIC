# environments/dev/vpc/terragrunt.hcl — Component 3 (VPC). Dev sizing: 2 AZs, single NAT.
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

# Dev-only overrides on top of the locked CIDRs in _envcommon/vpc.hcl.
inputs = {
  az_count               = local.env_vars.locals.az_count            # 2 in dev
  single_nat_gateway     = local.env_vars.locals.single_nat_gateway  # true in dev
  one_nat_gateway_per_az = !local.env_vars.locals.single_nat_gateway # false in dev
}
