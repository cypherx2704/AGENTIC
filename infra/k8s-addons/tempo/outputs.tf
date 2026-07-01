output "namespace" {
  description = "Namespace Tempo runs in."
  value       = var.namespace
}

output "chart_version" {
  description = "Installed tempo-distributed chart version."
  value       = helm_release.tempo.version
}

output "s3_bucket" {
  description = "S3 bucket backing Tempo trace blocks (Component 13: cypherx-tempo-traces-<env>)."
  value       = local.s3_bucket
}

output "otlp_grpc_endpoint" {
  description = "OTLP gRPC endpoint (Component 7 otel-tempo extensionProvider target). Must equal tempo-distributor.observability.svc.cluster.local:4317."
  value       = "tempo-distributor.${var.namespace}.svc.cluster.local:4317"
}

output "otlp_http_endpoint" {
  description = "OTLP HTTP endpoint (port 4318)."
  value       = "tempo-distributor.${var.namespace}.svc.cluster.local:4318"
}

output "retention_period" {
  description = "Configured trace retention (Component 13: 7d)."
  value       = var.retention_period
}
