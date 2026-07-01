###############################################################################
# RDS PostgreSQL module — input variables
#
# Component 5 (phase-01-infrastructure.md, lines 256-270):
#   Engine PostgreSQL 16
#   Instance db.r6g.xlarge (dev db.t3.medium)
#   Multi-AZ enabled prod / disabled dev
#   Storage 100GB gp3, autoscale to 1TB
#   Backup 7-day retention + PITR
#   Encryption KMS
#   Subnet group: private subnets only
#   Parameter group:
#     max_connections                     = 1000
#     shared_preload_libraries            = pg_stat_statements   (NOT vector)
#     log_min_duration_statement          = 500
#     idle_in_transaction_session_timeout = 60000
###############################################################################

variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "identifier" {
  description = "Optional explicit RDS identifier. Defaults to cypherx-<env>-postgres when null."
  type        = string
  default     = null
}

variable "engine_version" {
  description = "PostgreSQL major version. Component 5 pins 16."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "RDS instance class. Component 5: db.r6g.xlarge (prod), db.t3.medium (dev)."
  type        = string
  default     = "db.r6g.xlarge"
}

variable "multi_az" {
  description = "Multi-AZ deployment. Component 5: enabled (prod) / disabled (dev)."
  type        = bool
  default     = true
}

# --- Storage (Component 5: 100GB gp3 autoscale to 1TB) -----------------------

variable "allocated_storage_gb" {
  description = "Initial allocated storage in GB."
  type        = number
  default     = 100
}

variable "max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling in GB."
  type        = number
  default     = 1000
}

variable "storage_type" {
  description = "EBS storage type."
  type        = string
  default     = "gp3"
}

# --- Networking --------------------------------------------------------------

variable "private_subnet_ids" {
  description = "Private subnet IDs (3 AZs) for the DB subnet group. Private subnets ONLY (Component 5)."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Multi-AZ RDS requires at least two private subnets in distinct AZs."
  }
}

variable "security_group_ids" {
  description = "Security group IDs (e.g. sg-rds from the vpc module). Inbound 5432 from sg-eks-nodes only."
  type        = list(string)
}

# --- Credentials -------------------------------------------------------------
# Master password is NEVER hardcoded — sourced from Doppler/SSM via this variable.

variable "master_username" {
  description = "Master DB username."
  type        = string
  default     = "cypherx_admin"
}

variable "master_password" {
  description = "Master DB password. Sourced from Doppler (db/admin/master_password) / SSM — NEVER hardcoded."
  type        = string
  sensitive   = true
}

variable "database_name" {
  description = "Initial database created with the instance."
  type        = string
  default     = "cypherx_platform"
}

variable "port" {
  description = "PostgreSQL port."
  type        = number
  default     = 5432
}

# --- Backup / PITR (Component 5: 7-day retention + PITR) ----------------------

variable "backup_retention_days" {
  description = "Automated backup retention in days. Component 5: 7."
  type        = number
  default     = 7
}

variable "backup_window" {
  description = "Daily backup window (UTC)."
  type        = string
  default     = "03:00-04:00"
}

variable "maintenance_window" {
  description = "Weekly maintenance window (UTC)."
  type        = string
  default     = "sun:04:30-sun:05:30"
}

# --- Encryption --------------------------------------------------------------

variable "kms_key_arn" {
  description = "Optional KMS key ARN for storage + PI encryption. When null, a dedicated key is created."
  type        = string
  default     = null
}

# --- Parameter group overrides (Component 5 defaults locked in main.tf) -------

variable "max_connections" {
  description = "max_connections. Component 5: 1000 (was 500)."
  type        = number
  default     = 1000
}

variable "log_min_duration_statement_ms" {
  description = "log_min_duration_statement (ms). Component 5: 500 — log slow queries >= 500ms."
  type        = number
  default     = 500
}

variable "idle_in_transaction_session_timeout_ms" {
  description = "idle_in_transaction_session_timeout (ms). Component 5: 60000 — kill idle txns (prevents RLS context leak)."
  type        = number
  default     = 60000
}

variable "extra_parameters" {
  description = "Additional DB parameters to merge into the parameter group (advanced)."
  type = list(object({
    name         = string
    value        = string
    apply_method = optional(string, "immediate")
  }))
  default = []
}

# --- Observability -----------------------------------------------------------

variable "performance_insights_enabled" {
  description = "Enable RDS Performance Insights."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Protect the instance from deletion. Recommended true in staging/prod."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
