variable "environment" {
  description = "Environment name (dev | staging | prod). Selects the Doppler config (e.g., dev|stg|prd) the per-namespace service tokens are scoped to."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "namespace" {
  description = "Namespace the Doppler operator runs in."
  type        = string
  default     = "doppler-operator-system"
}

variable "create_namespace" {
  description = "Create the operator namespace via Helm (this namespace is operator-internal and not in the Component-6 set, so default true)."
  type        = bool
  default     = true
}

variable "chart_version" {
  description = "doppler/doppler-kubernetes-operator Helm chart version (pinned)."
  type        = string
  default     = "1.6.0"
}

variable "doppler_project" {
  description = "Doppler project the operator syncs from. Component 20: cypherx-platform."
  type        = string
  default     = "cypherx-platform"
}

variable "bootstrap_service_tokens" {
  description = <<-EOT
    Per-(env,namespace) Doppler service tokens, scoped to that namespace's config.
    Map of K8s-namespace -> Doppler service token. Provisioned by the Terraform
    `doppler` provider in the G3 doppler-bootstrap stack (NOT manual kubectl) and
    passed in here. Each value is written to a K8s Secret the operator reads.
    Keys MUST come from the Doppler provider output; values are NEVER hardcoded.
  EOT
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "create_example_dopplersecret" {
  description = "Render the reference auth-service-secrets DopplerSecret CRD in shared-core (Component 11 example). Off by default — it's documentation/reference, real DopplerSecrets ship with each service."
  type        = bool
  default     = false
}
