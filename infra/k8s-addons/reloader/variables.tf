variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "chart_version" {
  description = "stakater/reloader Helm chart version (pinned)."
  type        = string
  default     = "1.0.121"
}

variable "namespace" {
  description = "Namespace for reloader."
  type        = string
  default     = "kube-system"
}
