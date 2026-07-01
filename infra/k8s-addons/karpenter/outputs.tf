output "namespace" {
  description = "Namespace the Karpenter controller runs in."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed Karpenter chart version."
  value       = helm_release.karpenter.version
}

output "ec2nodeclass_name" {
  description = "Name of the shared compute EC2NodeClass."
  value       = "cypherx-compute"
}

output "nodepool_names" {
  description = "Karpenter NodePool names created (core, agent, tools). No observability/system NodePools — those are managed node groups (non-overlap guard)."
  value       = ["core", "agent", "tools"]
}

output "spot_enabled_nodepools" {
  description = "NodePools that allow spot capacity (Component 4: agent + tools)."
  value       = ["agent", "tools"]
}

output "managed_nodegroup_roles_excluded" {
  description = "Roles intentionally NOT given a Karpenter NodePool (managed by EKS node groups; non-overlap guard)."
  value       = ["system", "observability"]
}
