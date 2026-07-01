variable "environment" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "namespace" {
  description = "Namespace for the observability stack (Component 6: observability, istio-injection disabled)."
  type        = string
  default     = "observability"
}

variable "create_namespace" {
  description = "Create the namespace via Helm. False when the namespaces module owns it (recommended)."
  type        = bool
  default     = false
}

variable "chart_version" {
  description = "prometheus-community/kube-prometheus-stack chart version (pinned)."
  type        = string
  default     = "61.3.2"
}

variable "storage_class" {
  description = "StorageClass for Prometheus/Grafana PVCs. Component 5/13: gp3."
  type        = string
  default     = "gp3"
}

variable "prometheus_pvc_size" {
  description = "Prometheus PVC size. Component 13: 50GB (gp3)."
  type        = string
  default     = "50Gi"
}

variable "grafana_pvc_size" {
  description = "Grafana PVC size. Component 13: 10GB."
  type        = string
  default     = "10Gi"
}

variable "prometheus_retention" {
  description = "Prometheus local TSDB retention. Long-term storage is Loki/Tempo S3; Prometheus keeps a working window."
  type        = string
  default     = "15d"
}

variable "grafana_host" {
  description = "Grafana hostname (grafana.<env>.cypherx.ai — internal ALB, VPN-only per Component 5)."
  type        = string
  default     = ""
}

variable "grafana_admin_password" {
  description = "Grafana admin password. Sourced from Doppler — NEVER hardcoded. If empty, the chart generates one into a Secret (rotate via Doppler in prod)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "node_selector" {
  description = "nodeSelector pinning Prometheus/Alertmanager onto the fixed observability managed node group (Component 4: PVCs are pinned, consolidation breaks EBS attach)."
  type        = map(string)
  default     = { "node-role" = "observability" }
}

variable "extra_values" {
  description = "Additional raw YAML values merged last into the kube-prometheus-stack release."
  type        = string
  default     = ""
}
