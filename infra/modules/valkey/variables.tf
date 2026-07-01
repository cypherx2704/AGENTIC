###############################################################################
# ElastiCache Valkey module — input variables
#
# Component 5 (phase-01-infrastructure.md, lines 282-290):
#   Engine    valkey 7.x
#   Cluster   3 nodes (prod), 1 node (dev)
#   Node type cache.r6g.large (prod), cache.t3.micro (dev)
#   Multi-AZ  enabled (prod)
#   TLS       enabled
#   Auth      AUTH token (stored in Doppler)
###############################################################################

variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "name" {
  description = "Optional explicit replication-group ID. Defaults to cypherx-<env>-valkey when null."
  type        = string
  default     = null
}

variable "engine_version" {
  description = "Valkey engine version. Component 5: 7.x."
  type        = string
  default     = "7.2"
}

variable "node_type" {
  description = "Cache node type. Component 5: cache.r6g.large (prod), cache.t3.micro (dev)."
  type        = string
  default     = "cache.r6g.large"
}

variable "node_count" {
  description = "Number of nodes in the replication group. Component 5: 3 (prod), 1 (dev)."
  type        = number
  default     = 3

  validation {
    condition     = var.node_count >= 1
    error_message = "node_count must be >= 1."
  }
}

variable "multi_az_enabled" {
  description = "Multi-AZ. Component 5: enabled (prod). Requires automatic_failover (node_count > 1)."
  type        = bool
  default     = true
}

variable "port" {
  description = "Valkey port."
  type        = number
  default     = 6379
}

# --- Networking --------------------------------------------------------------

variable "private_subnet_ids" {
  description = "Private subnet IDs for the cache subnet group."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 1
    error_message = "At least one private subnet is required."
  }
}

variable "security_group_ids" {
  description = "Security group IDs (e.g. sg-valkey). Inbound 6379 from sg-eks-nodes only."
  type        = list(string)
}

# --- Security (TLS + AUTH) ---------------------------------------------------

variable "auth_token" {
  description = "Valkey AUTH token. Sourced from Doppler — NEVER hardcoded. 16-128 printable chars."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.auth_token) >= 16 && length(var.auth_token) <= 128
    error_message = "auth_token must be 16-128 characters (ElastiCache requirement)."
  }
}

variable "kms_key_arn" {
  description = "Optional KMS key ARN for at-rest encryption. When null, a dedicated key is created."
  type        = string
  default     = null
}

# --- Maintenance / snapshots -------------------------------------------------

variable "snapshot_retention_days" {
  description = "Daily snapshot retention in days (0 disables; not supported on t3.micro)."
  type        = number
  default     = 7
}

variable "maintenance_window" {
  description = "Weekly maintenance window (UTC)."
  type        = string
  default     = "sun:05:30-sun:06:30"
}

variable "snapshot_window" {
  description = "Daily snapshot window (UTC)."
  type        = string
  default     = "04:00-05:00"
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
