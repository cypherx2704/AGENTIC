# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/eks.hcl — shared inputs for the EKS stack (Component 4).
# One cluster per environment: cypherx-dev / cypherx-staging / cypherx-prod. Managed node groups ONLY for
# system-nodes + observability (fixed, ON_DEMAND). Karpenter owns core/agent/tools — do NOT create managed NGs for
# those (the two scalers fight — Component 4 guard).
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//eks-cluster"
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    vpc_id             = "vpc-00000000000000000"
    private_subnet_ids = ["subnet-aaaa", "subnet-bbbb", "subnet-cccc"]
    public_subnet_ids  = ["subnet-dddd", "subnet-eeee", "subnet-ffff"]
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  cluster_name    = "cypherx-${local.env}"
  cluster_version = "1.30" # Component 4: K8s 1.30.

  vpc_id                   = dependency.vpc.outputs.vpc_id
  subnet_ids               = dependency.vpc.outputs.private_subnet_ids
  control_plane_subnet_ids = dependency.vpc.outputs.private_subnet_ids

  # Component 4: API server PRIVATE ONLY, public endpoint disabled.
  cluster_endpoint_public_access  = false
  cluster_endpoint_private_access = true

  # Component 4: control-plane logging to CloudWatch.
  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  # OIDC / IRSA enabled (Component 4).
  enable_irsa = true

  # AWS-managed add-ons (Component 4).
  cluster_addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni    = { most_recent = true }
  }

  # ----- Managed node groups (fixed, ON_DEMAND). Sizes/instances come from env.hcl. -----
  # NOTE: core/agent/tools are Karpenter NodePools (Component 17b CRDs, owned by k8s-addons), NOT here.
  managed_node_groups = local.env_vars.locals.eks_managed_node_groups

  # Karpenter discovery tag so its NodePools can find subnets/SGs.
  node_security_group_tags = {
    "karpenter.sh/discovery" = "cypherx-${local.env}"
  }
}
