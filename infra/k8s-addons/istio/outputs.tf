output "istio_version" {
  description = "Pinned Istio chart/control-plane version."
  value       = var.istio_version
}

output "istio_namespace" {
  description = "Namespace hosting istio-base + istiod."
  value       = var.istio_namespace
}

output "gateway_namespace" {
  description = "Namespace hosting the Istio ingress gateway."
  value       = var.gateway_namespace
}

output "tracing_sample_percentage" {
  description = "Effective mesh-wide trace sampling percentage applied (100 dev/staging, 10 prod)."
  value       = local.sample_pct
}

output "otel_tempo_endpoint" {
  description = "OTLP gRPC endpoint bound to the otel-tempo extension provider."
  value       = "${var.tempo_otlp_grpc_endpoint}:${var.tempo_otlp_grpc_port}"
}

output "peer_authentication_mode" {
  description = "Global PeerAuthentication mTLS mode (STRICT, with scoped PERMISSIVE/ DISABLE exceptions)."
  value       = "STRICT"
}
