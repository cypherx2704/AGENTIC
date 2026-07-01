# ---------------------------------------------------------------------------------------------------------------------
# modules/kafka-topics/variables.tf — Component 17.
# ---------------------------------------------------------------------------------------------------------------------

variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string
}

variable "bootstrap_servers" {
  description = "MSK SASL_SSL bootstrap broker list (host:port). SCRAM-SHA-512 listener is port 9096."
  type        = list(string)
}

variable "tls_enabled" {
  description = "Whether the broker connection uses TLS (MSK in-transit encryption — always true for MSK)."
  type        = bool
  default     = true
}

variable "sasl_mechanism" {
  description = "SASL mechanism for the admin connection. MSK uses SCRAM-SHA-512."
  type        = string
  default     = "scram-sha512"
}

variable "sasl_username" {
  description = "SASL SCRAM admin username (MSK). Sourced from Doppler via TF_VAR_kafka_sasl_username. NEVER hardcoded."
  type        = string
  sensitive   = true
}

variable "sasl_password" {
  description = "SASL SCRAM admin password (MSK). Sourced from Doppler via TF_VAR_kafka_sasl_password. NEVER hardcoded."
  type        = string
  sensitive   = true
}

variable "default_replication_factor" {
  description = "Replication factor for all core topics. Component 17: 3 on the MSK 3-broker cluster."
  type        = number
  default     = 3
}

variable "dlq_retention_ms" {
  description = "Retention for DLQ topics. Component 17: 30 days."
  type        = number
  default     = 2592000000 # 30 days
}
