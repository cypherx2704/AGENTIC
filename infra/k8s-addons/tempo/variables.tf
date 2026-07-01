variable "environment" {
  description = "Environment name (dev | staging | prod). Selects the S3 bucket suffix and dev-single vs prod-distributed topology."
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
  description = "grafana/tempo-distributed Helm chart version (pinned). Component 7 references tempo-distributor.observability.svc, so the distributed chart is used."
  type        = string
  default     = "1.10.3"
}

variable "s3_bucket_name" {
  description = "S3 bucket for Tempo trace blocks. Component 13: cypherx-tempo-traces-<env>. If empty, derived from environment."
  type        = string
  default     = ""
}

variable "aws_region" {
  description = "AWS region for the Tempo S3 bucket. Component 3: us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "irsa_role_arn" {
  description = "IRSA role ARN granting Tempo read/write to the S3 bucket. Provisioned by the G3 IAM stack. No static keys."
  type        = string
  default     = ""
}

variable "retention_period" {
  description = "Trace retention. Component 13: 7 days."
  type        = string
  default     = "168h" # 7d
}

variable "node_selector" {
  description = "Pin Tempo onto the fixed observability managed NG (Component 4 PVC/EBS consolidation guard)."
  type        = map(string)
  default     = { "node-role" = "observability" }
}

variable "extra_values" {
  description = "Additional raw YAML values merged last into the Tempo release."
  type        = string
  default     = ""
}
