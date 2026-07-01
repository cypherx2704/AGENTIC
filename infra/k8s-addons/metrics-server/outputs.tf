output "namespace" {
  description = "Namespace metrics-server is installed into."
  value       = var.namespace
}

output "chart_version" {
  description = "Pinned metrics-server chart version."
  value       = var.chart_version
}
