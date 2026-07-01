# environments/dev/eks/terragrunt.hcl — Component 4 (EKS). cypherx-dev cluster.
# Managed node groups (system-nodes, observability) sized in env.hcl. core/agent/tools = Karpenter (k8s-addons).
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/eks.hcl"
  expose = true
}

# No dev-specific overrides beyond what env.hcl drives through _envcommon (managed_node_groups sizes).
inputs = {}
