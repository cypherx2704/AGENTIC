variable "bucket_name" {
  description = "Name of the S3 bucket that stores Terraform state. Convention: cypherx-terraform-state-<account-id> (Component 2)."
  type        = string
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table used for Terraform state locking (Component 2)."
  type        = string
  default     = "cypherx-terraform-locks"
}

variable "kms_key_arn" {
  description = "Optional KMS key ARN for SSE-KMS. When null, the AWS-managed aws/s3 key (alias aws/s3) is used as specified by Component 2."
  type        = string
  default     = null
}

variable "noncurrent_version_expiration_days" {
  description = "Number of days after which noncurrent (previous) object versions are permanently deleted (Component 2: 90 days)."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
