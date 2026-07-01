variable "environment" {
  description = "Environment name (dev | staging | prod). Stamped as the istio-injection-independent environment label on every namespace."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "common_labels" {
  description = "Additional labels merged onto every CypherX namespace (e.g., team, cost-center). Component-6 istio-injection labels are set per-namespace and cannot be overridden here."
  type        = map(string)
  default     = {}
}
