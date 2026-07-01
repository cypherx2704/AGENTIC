variable "bucket_name" {
  description = "Bucket name. For observability use cypherx-loki-logs-<env> / cypherx-tempo-traces-<env> (Component 13)."
  type        = string
}

variable "versioning_enabled" {
  description = "Enable object versioning. Loki/Tempo object stores typically leave this false (immutable, lifecycle-managed objects)."
  type        = bool
  default     = false
}

variable "sse_algorithm" {
  description = "Server-side encryption algorithm: aws:kms (default) or AES256."
  type        = string
  default     = "aws:kms"

  validation {
    condition     = contains(["aws:kms", "AES256"], var.sse_algorithm)
    error_message = "sse_algorithm must be aws:kms or AES256."
  }
}

variable "kms_key_arn" {
  description = "Optional CMK ARN for SSE-KMS. Null uses the AWS-managed aws/s3 key."
  type        = string
  default     = null
}

variable "expiration_days" {
  description = "Expire current objects after N days. 0 disables expiration. Set to the retention window of the consumer (Loki 30, Tempo 7)."
  type        = number
  default     = 0
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent versions expire (only relevant when versioning_enabled)."
  type        = number
  default     = 30
}

variable "force_destroy" {
  description = "Allow Terraform to delete a non-empty bucket. Keep false for prod data buckets."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to the bucket."
  type        = map(string)
  default     = {}
}
