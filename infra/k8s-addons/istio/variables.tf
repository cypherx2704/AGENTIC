variable "env" {
  description = "Environment name (dev | staging | prod). Drives tracing sample rate."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "istio_version" {
  description = "Istio chart version. Pinned to the 1.22.x line (Component 7: latest stable at build time)."
  type        = string
  default     = "1.22.3"
}

variable "istio_namespace" {
  description = "Namespace for istiod + istio-base (control plane)."
  type        = string
  default     = "istio-system"
}

variable "gateway_namespace" {
  description = "Namespace for the Istio ingress gateway. Kong/ingress live in the `ingress` namespace (Component 6)."
  type        = string
  default     = "ingress"
}

variable "tempo_otlp_grpc_endpoint" {
  description = <<-EOT
    OTLP gRPC endpoint of the Tempo distributor (Component 13). The mesh exports
    traces here via the `otel-tempo` extension provider (Component 7). Host only —
    port is supplied separately so meshConfig keeps them split.
  EOT
  type        = string
  default     = "tempo-distributor.observability.svc.cluster.local"
}

variable "tempo_otlp_grpc_port" {
  description = "OTLP gRPC port on the Tempo distributor (Component 13: 4317)."
  type        = number
  default     = 4317
}

variable "tracing_sample_percentage" {
  description = <<-EOT
    Override for the mesh-wide trace sampling percentage. When null it is derived
    from `env`: 100.0 for dev/staging, 10.0 for prod (Component 7).
  EOT
  type        = number
  default     = null
}

variable "metrics_permissive_ports" {
  description = <<-EOT
    Ports that get a PERMISSIVE mTLS exception so the sidecar-less observability
    namespace can scrape over plain HTTP (Component 7). 15020 = Istio merged
    metrics port; 9090 = the app /metrics port convention. Everything else stays
    STRICT.
  EOT
  type        = list(number)
  default     = [15020, 9090]
}

variable "non_mesh_hosts" {
  description = <<-EOT
    Hosts in non-mesh namespaces (e.g. `data`) that sidecar'd callers reach and
    for which mTLS origination MUST be disabled (Component 7). pgbouncer is the
    canonical first entry; add ExternalName-resolved RDS/MSK endpoints here too.
    Do NOT weaken global PeerAuthentication — scope the exception to the host.
  EOT
  type = map(object({
    host = string
  }))
  default = {
    pgbouncer = {
      host = "pgbouncer.data.svc.cluster.local"
    }
  }
}
