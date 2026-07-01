###############################################################################
# MSK Kafka module — input variables
#
# Component 5 (phase-01-infrastructure.md, lines 292-301):
#   Brokers   3 (one per AZ)
#   Instance  kafka.m5.large (prod), kafka.t3.small (dev)
#   Volume    100GB per broker (gp3)
#   Version   3.6.x (latest stable)
#   TLS       enabled (in-transit encryption)
#   At-rest   KMS encrypted
#   SASL      SCRAM-SHA-512 auth
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
  description = "Optional explicit cluster name. Defaults to cypherx-<env>-kafka when null."
  type        = string
  default     = null
}

variable "kafka_version" {
  description = "Kafka version. Component 5: 3.6.x."
  type        = string
  default     = "3.6.0"
}

variable "broker_count" {
  description = "Number of broker nodes. Component 5: 3 (one per AZ). Must be a multiple of the AZ/subnet count."
  type        = number
  default     = 3

  validation {
    condition     = var.broker_count >= 3 && var.broker_count % 3 == 0
    error_message = "broker_count must be >= 3 and a multiple of 3 (one broker per AZ across 3 AZs)."
  }
}

variable "broker_instance_type" {
  description = "Broker instance type. Component 5: kafka.m5.large (prod), kafka.t3.small (dev)."
  type        = string
  default     = "kafka.m5.large"
}

variable "broker_volume_gb" {
  description = "EBS volume per broker in GB. Component 5: 100 (gp3)."
  type        = number
  default     = 100
}

variable "broker_volume_type" {
  description = "EBS volume type for broker storage."
  type        = string
  default     = "gp3"
}

# --- Networking --------------------------------------------------------------

variable "private_subnet_ids" {
  description = "Private subnet IDs (3 AZs) — one broker per AZ. Length must divide broker_count evenly."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) == 3
    error_message = "Exactly 3 private subnets (one per AZ) are required for the 3-broker MSK layout."
  }
}

variable "security_group_ids" {
  description = "Security group IDs (e.g. sg-kafka). Inbound 9092,9094 from sg-eks-nodes only."
  type        = list(string)
}

# --- Encryption --------------------------------------------------------------

variable "kms_key_arn" {
  description = "Optional KMS key ARN for at-rest encryption. When null, a dedicated key is created."
  type        = string
  default     = null
}

# --- SASL/SCRAM credentials --------------------------------------------------
# Username/password are NEVER hardcoded — sourced from Doppler/SSM via variables.

variable "scram_username" {
  description = "SASL/SCRAM-SHA-512 username for platform services. From Doppler — never hardcoded."
  type        = string
  default     = "cypherx_app"
}

variable "scram_password" {
  description = "SASL/SCRAM-SHA-512 password. From Doppler (kafka/sasl_password) — never hardcoded. Min 8 chars, no spaces/colons."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.scram_password) >= 8
    error_message = "scram_password must be at least 8 characters (MSK SCRAM requirement)."
  }
}

# --- Cluster config / monitoring ---------------------------------------------

variable "enhanced_monitoring" {
  description = "MSK enhanced monitoring level (DEFAULT | PER_BROKER | PER_TOPIC_PER_BROKER | PER_TOPIC_PER_PARTITION)."
  type        = string
  default     = "PER_TOPIC_PER_BROKER"
}

variable "min_insync_replicas" {
  description = "Cluster-level default min.insync.replicas. Component 17 topics also pin 2; matches replication=3."
  type        = number
  default     = 2
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
