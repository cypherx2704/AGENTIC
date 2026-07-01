# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/valkey.hcl — shared inputs for the Valkey (ElastiCache) stack (Component 5).
# Valkey 7.x, TLS enabled, AUTH token (from Doppler). Node count + node type + multi-AZ vary by env (env.hcl):
#   dev  = 1 node, cache.t3.micro, multi-AZ off
#   prod = 3 nodes, cache.r6g.large, multi-AZ on
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//valkey"
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    vpc_id             = "vpc-00000000000000000"
    private_subnet_ids = ["subnet-aaaa", "subnet-bbbb", "subnet-cccc"]
    eks_node_sg_id     = "sg-00000000000000000"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  cluster_name = "cypherx-${local.env}"

  engine         = "valkey"
  engine_version = "7.2" # Component 5: valkey 7.x.

  subnet_ids = dependency.vpc.outputs.private_subnet_ids
  vpc_id     = dependency.vpc.outputs.vpc_id

  # Component 5: TLS enabled.
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true

  # Component 5: AUTH token stored in Doppler — passed via var.auth_token (Doppler-sourced), NOT hardcoded.

  port = 6379

  # Component 3 sg-valkey: inbound 6379 from sg-eks-nodes only.
  allowed_security_group_ids = [dependency.vpc.outputs.eks_node_sg_id]
}
