variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "kong_chart_version" {
  description = "kong/kong Helm chart version (Component 8: Kong 3.6.x, DB-less)."
  type        = string
  default     = "2.38.0" # ships Kong gateway 3.6.x
}

variable "namespace" {
  description = "Namespace for Kong. Runs in `ingress` (istio-injection: enabled) so Kong->backend is mTLS via Istio (Component 6/8)."
  type        = string
  default     = "ingress"
}

variable "replica_count" {
  description = "Kong proxy replicas. Health checklist requires 2+ (Component 8)."
  type        = number
  default     = 2
}

variable "acm_certificate_arn" {
  description = <<-EOT
    ACM certificate ARN attached to the ALB (per-env wildcard *.<env>.cypherx.ai,
    Component 5). The ALB terminates TLS with this cert. Sourced from the dns/ACM
    stack output (or SSM); not a secret but env-varying.
  EOT
  type        = string
}

variable "alb_scheme" {
  description = "ALB scheme. Public Kong is internet-facing (Component 10)."
  type        = string
  default     = "internet-facing"
}
