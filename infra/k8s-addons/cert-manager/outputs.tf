output "namespace" {
  description = "Namespace cert-manager is installed into."
  value       = var.namespace
}

output "chart_version" {
  description = "Pinned cert-manager chart version."
  value       = var.chart_version
}

output "cluster_issuer_name" {
  description = "Name of the ACME ClusterIssuer for internal dashboard TLS."
  value       = "letsencrypt-prod"
}
