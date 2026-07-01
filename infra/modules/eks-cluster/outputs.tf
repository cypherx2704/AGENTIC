###############################################################################
# EKS cluster module — outputs
###############################################################################

output "cluster_name" {
  description = "EKS cluster name (cypherx-<env>)."
  value       = module.eks.cluster_name
}

output "cluster_arn" {
  description = "EKS cluster ARN."
  value       = module.eks.cluster_arn
}

output "cluster_endpoint" {
  description = "Private API server endpoint (reachable only inside the VPC / via VPN)."
  value       = module.eks.cluster_endpoint
}

output "cluster_version" {
  description = "Kubernetes control-plane version."
  value       = module.eks.cluster_version
}

output "cluster_certificate_authority_data" {
  description = "Base64 CA cert for the cluster API server (for kubeconfig)."
  value       = module.eks.cluster_certificate_authority_data
}

output "cluster_security_group_id" {
  description = "Cluster security group ID created by EKS (control-plane <-> nodes)."
  value       = module.eks.cluster_security_group_id
}

output "node_security_group_id" {
  description = "Shared node security group ID created by the EKS module."
  value       = module.eks.node_security_group_id
}

# --- OIDC / IRSA -------------------------------------------------------------

output "oidc_provider_arn" {
  description = "IAM OIDC provider ARN for IRSA. Per-service IRSA roles (Phase 2+) trust this."
  value       = module.eks.oidc_provider_arn
}

output "cluster_oidc_issuer_url" {
  description = "OIDC issuer URL of the cluster."
  value       = module.eks.cluster_oidc_issuer_url
}

# --- Node groups -------------------------------------------------------------

output "eks_managed_node_groups" {
  description = "Managed node group attributes (system-nodes, observability)."
  value       = module.eks.eks_managed_node_groups
}

output "kms_key_arn" {
  description = "KMS key ARN used for EKS secrets envelope encryption."
  value       = local.eks_kms_key_arn
}
