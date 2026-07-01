# ---------------------------------------------------------------------------------------------------------------------
# modules/postgres-bootstrap/variables.tf — Component 16.
# ---------------------------------------------------------------------------------------------------------------------

variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string
}

variable "pg_host" {
  description = "RDS PostgreSQL endpoint hostname (from the postgresql stack)."
  type        = string
}

variable "pg_port" {
  description = "RDS PostgreSQL port."
  type        = number
  default     = 5432
}

variable "pg_superuser" {
  description = "RDS master/admin username (rds_superuser) used to bootstrap. From the postgresql stack."
  type        = string
}

variable "pg_superuser_password" {
  description = <<-EOT
    Password for the RDS master/admin user. Sourced from the AWS-managed master secret
    (master_user_secret_arn) or injected via TF_VAR_pg_superuser_password from Doppler. NEVER hardcoded.
  EOT
  type        = string
  sensitive   = true
  default     = "" # resolved from master_user_secret_arn when empty.
}

variable "master_user_secret_arn" {
  description = "ARN of the AWS-managed RDS master secret. When set, the module reads the password from it."
  type        = string
  default     = ""
}

variable "database_name" {
  description = "Main application database to create."
  type        = string
  default     = "cypherx_platform"
}

variable "sslmode" {
  description = "libpq sslmode for the bootstrap connection. RDS requires encrypted transit."
  type        = string
  default     = "require"
}

# ---------------------------------------------------------------------------------------------------------------------
# Per-service runtime + DDL passwords. One pair per service schema. Sourced from Doppler:
#   runtime -> db/<service>/runtime_password   (Contract 14, Component 14)
#   ddl     -> db/<service>/ddl_password        (Contract 14, Component 14)
# Injected as TF_VAR_runtime_passwords / TF_VAR_ddl_passwords (maps) — no defaults, so a missing value fails loudly.
# Keys MUST match the service keys in local.services: auth, llms, guardrails, memory, rag, xagent, platform-mgmt
# (the platform service's Doppler name is "platform-mgmt" per Contract 20, even though its schema is "platform").
# ---------------------------------------------------------------------------------------------------------------------
variable "runtime_passwords" {
  description = "Map service-key => runtime user password (db/<svc>/runtime_password from Doppler)."
  type        = map(string)
  sensitive   = true
}

variable "ddl_passwords" {
  description = "Map service-key => DDL user password (db/<svc>/ddl_password from Doppler)."
  type        = map(string)
  sensitive   = true
}
