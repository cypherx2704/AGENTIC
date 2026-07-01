output "namespace" {
  description = "Namespace Loki runs in."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed Loki chart version."
  value       = helm_release.loki.version
}

output "s3_bucket" {
  description = "S3 bucket backing Loki chunks/index (Component 13: cypherx-loki-logs-<env>)."
  value       = local.s3_bucket
}

output "deployment_mode" {
  description = "SingleBinary (dev) or SimpleScalable (staging/prod)."
  value       = local.single_node ? "SingleBinary" : "SimpleScalable"
}

output "gateway_endpoint" {
  description = "In-cluster Loki push/query gateway endpoint (Promtail/Grafana target)."
  value       = "http://loki-gateway.${var.namespace}.svc.cluster.local"
}

output "retention_period" {
  description = "Configured log retention."
  value       = var.retention_period
}
