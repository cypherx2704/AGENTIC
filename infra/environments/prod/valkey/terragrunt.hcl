# environments/prod/valkey/terragrunt.hcl — Component 5 (ElastiCache Valkey). Prod: 3-node cluster, multi-AZ.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/valkey.hcl"
  expose = true
}

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

inputs = {
  node_type        = local.env_vars.locals.valkey_node_type
  num_cache_nodes  = local.env_vars.locals.valkey_num_cache_nodes
  multi_az_enabled = local.env_vars.locals.valkey_multi_az
}
