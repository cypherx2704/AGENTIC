variable "environment" {
  description = "Environment name (dev | staging | prod). Selects single-binary (dev) vs scalable (prod) deployment mode and the S3 bucket suffix."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "namespace" {
  description = "Observability namespace (Component 6: observability, istio-injection disabled)."
  type        = string
  default     = "observability"
}

variable "create_namespace" {
  description = "Create the namespace via Helm. False when the namespaces module owns it."
  type        = bool
  default     = false
}

variable "chart_version" {
  description = "grafana/loki Helm chart version (pinned)."
  type        = string
  default     = "6.6.4"
}

variable "s3_bucket_name" {
  description = "S3 bucket for Loki chunks/index. Component 13: cypherx-loki-logs-<env>. If empty, derived from environment."
  type        = string
  default     = ""
}

variable "aws_region" {
  description = "AWS region for the Loki S3 bucket. Component 3: us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "irsa_role_arn" {
  description = "IRSA role ARN granting Loki (read/write) access to the S3 bucket. Provisioned by the G3 IAM stack. Empty in dev if using node-role creds (not recommended)."
  type        = string
  default     = ""
}

variable "retention_period" {
  description = "Log retention. Component 13: 30 days."
  type        = string
  default     = "720h" # 30d
}

variable "ingestion_rate_mb" {
  description = "Per-tenant ingestion rate limit (MB/s). Component 13: ingestion_rate_mb=10 per service."
  type        = number
  default     = 10
}

variable "ingestion_burst_size_mb" {
  description = "Per-tenant ingestion burst (MB). Component 13: ingestion_burst_size_mb=20 per service."
  type        = number
  default     = 20
}

variable "node_selector" {
  description = "Pin Loki onto the fixed observability managed NG (Component 4 — PVC/EBS consolidation guard)."
  type        = map(string)
  default     = { "node-role" = "observability" }
}

variable "extra_values" {
  description = "Additional raw YAML values merged last into the Loki release."
  type        = string
  default     = ""
}
