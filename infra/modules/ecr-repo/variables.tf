variable "name" {
  description = "ECR repository name, e.g. cypherx/auth-service (Component 5)."
  type        = string
}

variable "image_tag_mutability" {
  description = "IMMUTABLE (recommended — Component 18 uses immutable sha-/semver tags) or MUTABLE."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["IMMUTABLE", "MUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be IMMUTABLE or MUTABLE."
  }
}

variable "scan_on_push" {
  description = "Enable image vulnerability scan on push (Component 5)."
  type        = bool
  default     = true
}

variable "encryption_type" {
  description = "ECR encryption type: KMS or AES256."
  type        = string
  default     = "KMS"
}

variable "kms_key_arn" {
  description = "Optional CMK ARN for KMS encryption. Null uses the AWS-managed ECR key."
  type        = string
  default     = null
}

variable "untagged_expire_days" {
  description = "Expire untagged images after N days (Component 5: 14)."
  type        = number
  default     = 14
}

variable "keep_last_tagged" {
  description = "Number of most-recent tagged images to keep (Component 5: keep last N)."
  type        = number
  default     = 30
}

variable "tag_prefixes_to_keep" {
  description = "Tag prefixes the keep-last-N rule applies to. Component 18 tags: sha-, v (semver), pr-."
  type        = list(string)
  default     = ["sha-", "v", "pr-"]
}

variable "pull_principal_arns" {
  description = "Additional IAM principal ARNs granted pull access via the repository policy (e.g. cross-account self-hosted). Empty = rely on identity-based policies (EKS node role) only."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to the repository."
  type        = map(string)
  default     = {}
}
