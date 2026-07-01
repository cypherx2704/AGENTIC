variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "chart_version" {
  description = "cert-manager/cert-manager Helm chart version (pinned)."
  type        = string
  default     = "v1.15.1"
}

variable "namespace" {
  description = "Namespace for cert-manager."
  type        = string
  default     = "cert-manager"
}

variable "acme_email" {
  description = <<-EOT
    Contact email for the ACME (Let's Encrypt) account on the letsencrypt-prod
    ClusterIssuer. Used for expiry/abuse notices. Operational contact, not a
    secret.
  EOT
  type        = string
}

variable "letsencrypt_acme_server" {
  description = "ACME directory URL for the letsencrypt-prod ClusterIssuer."
  type        = string
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}

variable "solver_ingress_class" {
  description = <<-EOT
    Ingress class used by the HTTP-01 solver. cert-manager is scoped to internal
    developer-facing dashboard ingresses only (Component 9), which are fronted by
    Kong/internal ingress — NOT the ACM-terminated public ALB.
  EOT
  type        = string
  default     = "kong"
}
