variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "chart_version" {
  description = "external-dns/external-dns Helm chart version (pinned)."
  type        = string
  default     = "1.14.5"
}

variable "namespace" {
  description = "Namespace for external-dns."
  type        = string
  default     = "kube-system"
}

variable "irsa_role_arn" {
  description = <<-EOT
    ARN of the ExternalDNS IRSA role (from modules/iam). Grants the Route53
    permissions (ChangeResourceRecordSets on the cypherx.ai hosted zone,
    ListHostedZones, ListResourceRecordSets).
  EOT
  type        = string
}

variable "service_account_name" {
  description = "Name of the external-dns ServiceAccount (must match the IRSA trust)."
  type        = string
  default     = "external-dns"
}

variable "domain_filter" {
  description = "Hosted zone domain external-dns manages records under (Component 5: cypherx.ai)."
  type        = string
  default     = "cypherx.ai"
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID for cypherx.ai. Sourced from the dns stack output."
  type        = string
}

variable "txt_owner_id" {
  description = <<-EOT
    Ownership TXT registry id so multiple clusters/envs never fight over the same
    Route53 records. Defaults to cypherx-<env>.
  EOT
  type        = string
  default     = null
}
