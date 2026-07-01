variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "chart_version" {
  description = "metrics-server/metrics-server Helm chart version (pinned)."
  type        = string
  default     = "3.12.1"
}

variable "namespace" {
  description = "Namespace for metrics-server."
  type        = string
  default     = "kube-system"
}

variable "replicas" {
  description = "metrics-server replicas. 2 for HA in prod, 1 elsewhere."
  type        = number
  default     = 1
}
