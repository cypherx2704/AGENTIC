# Component 5 — reusable ECR repository.
# Scan-on-push enabled; lifecycle: expire untagged after 14 days, keep last N
# tagged images.

resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = var.image_tag_mutability

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  encryption_configuration {
    encryption_type = var.encryption_type
    kms_key         = var.encryption_type == "KMS" ? var.kms_key_arn : null
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after ${var.untagged_expire_days} days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_expire_days
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep only the last ${var.keep_last_tagged} tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = var.tag_prefixes_to_keep
          countType     = "imageCountMoreThan"
          countNumber   = var.keep_last_tagged
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

# Optional resource-based pull policy for extra principals (cross-account /
# self-hosted runners). EKS node pull works via the node role's identity policy.
resource "aws_ecr_repository_policy" "pull" {
  count      = length(var.pull_principal_arns) > 0 ? 1 : 0
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowPull"
        Effect = "Allow"
        Principal = {
          AWS = var.pull_principal_arns
        }
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:BatchCheckLayerAvailability",
        ]
      },
    ]
  })
}
