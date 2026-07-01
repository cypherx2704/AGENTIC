output "namespace" {
  description = "Namespace the controller is installed into."
  value       = var.namespace
}

output "chart_version" {
  description = "Pinned aws-load-balancer-controller chart version."
  value       = var.chart_version
}

output "service_account_name" {
  description = "Name of the controller ServiceAccount (IRSA-bound)."
  value       = var.service_account_name
}
