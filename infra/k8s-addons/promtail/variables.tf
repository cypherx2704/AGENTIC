variable "environment" {
  description = "Environment name (dev | staging | prod). Stamped as the 'environment' Loki label (Contract 6)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "namespace" {
  description = "Observability namespace. Promtail runs as a DaemonSet on all nodes; the controller lives here."
  type        = string
  default     = "observability"
}

variable "create_namespace" {
  description = "Create the namespace via Helm. False when the namespaces module owns it."
  type        = bool
  default     = false
}

variable "chart_version" {
  description = "grafana/promtail Helm chart version (pinned)."
  type        = string
  default     = "6.16.6"
}

variable "loki_push_url" {
  description = "Loki push endpoint. Defaults to the in-cluster Loki gateway in the observability namespace."
  type        = string
  default     = ""
}

variable "extra_values" {
  description = "Additional raw YAML values merged last into the Promtail release."
  type        = string
  default     = ""
}
