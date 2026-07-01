output "namespace" {
  description = "Namespace the observability stack runs in."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed kube-prometheus-stack chart version."
  value       = helm_release.kube_prometheus_stack.version
}

output "grafana_host" {
  description = "Grafana hostname (internal ALB / VPN-only)."
  value       = local.grafana_host
}

output "prometheus_service" {
  description = "In-cluster Prometheus service DNS (for ServiceMonitor/remote targets)."
  value       = "kube-prometheus-stack-prometheus.${var.namespace}.svc.cluster.local:9090"
}

output "preimported_dashboards" {
  description = "List of pre-imported Grafana dashboard names (Component 13)."
  value       = keys(local.preimported_dashboards)
}
