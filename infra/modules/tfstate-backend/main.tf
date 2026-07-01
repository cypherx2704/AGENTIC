# Component 2 — Terraform Remote State backend.
# S3 bucket (versioned, SSE-KMS, public access blocked, noncurrent versions
# expire after 90 days) + DynamoDB lock table (PAY_PER_REQUEST, hash key LockID).
#
# This module bootstraps the backend itself, so its OWN state is created with a
# local backend on first apply and then migrated into the bucket it creates
# (documented in README). Do NOT add a backend "s3" block here.

resource "aws_s3_bucket" "state" {
  bucket = var.bucket_name

  # The state bucket is foundational; guard against accidental `terraform destroy`.
  lifecycle {
    prevent_destroy = true
  }

  tags = var.tags
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      # SSE-KMS. When no customer key is provided, fall back to the AWS-managed
      # aws/s3 key (sse_algorithm = aws:kms with no kms_master_key_id).
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  # Required when the bucket has versioning enabled (provider >= 5).
  depends_on = [aws_s3_bucket_versioning.state]

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    # Clean up incomplete multipart uploads so they do not accumulate cost.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Deny any non-TLS access to the state bucket (defence in depth alongside the
# public-access block above).
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.state.arn,
          "${aws_s3_bucket.state.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_dynamodb_table" "locks" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = var.tags
}
