variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of dev, staging, prod."
  }
}

variable "root_domain" {
  description = "Apex domain for the platform (Component 5: cypherx.ai). Delegated from the registrar."
  type        = string
  default     = "cypherx.ai"
}

variable "create_hosted_zone" {
  description = "Whether THIS stack creates the cypherx.ai public hosted zone. The zone is account-global and shared across envs, so exactly one stack (prod, by convention) should own it; others set false and pass hosted_zone_id."
  type        = bool
  default     = false
}

variable "hosted_zone_id" {
  description = "Existing Route53 hosted zone ID for root_domain. Required when create_hosted_zone = false."
  type        = string
  default     = null
}

variable "public_alb_dns_name" {
  description = "DNS name of this env's public (internet-facing) ALB for Kong. Record targets api.<env> and auth.<env> at it. Typically supplied by external-dns at runtime; pass here for the Terraform-managed records."
  type        = string
  default     = null
}

variable "public_alb_zone_id" {
  description = "Hosted zone ID of the public ALB (for ALIAS A records). Required when public_alb_dns_name is set."
  type        = string
  default     = null
}

variable "internal_alb_dns_name" {
  description = "DNS name of this env's internal (VPN-only) ALB for argocd/grafana."
  type        = string
  default     = null
}

variable "internal_alb_zone_id" {
  description = "Hosted zone ID of the internal ALB (for ALIAS A records)."
  type        = string
  default     = null
}

variable "manage_app_records" {
  description = "Whether Terraform manages the api/auth/argocd/grafana ALIAS records. Set false when external-dns owns them at runtime (the ACM cert + zone are still managed here)."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to DNS/ACM resources."
  type        = map(string)
  default     = {}
}
