output "repository_url" {
  description = "URL of the ECR repository (account.dkr.ecr.region.amazonaws.com/name)."
  value       = aws_ecr_repository.this.repository_url
}

output "repository_arn" {
  description = "ARN of the ECR repository."
  value       = aws_ecr_repository.this.arn
}

output "repository_name" {
  description = "Name of the ECR repository."
  value       = aws_ecr_repository.this.name
}

output "registry_id" {
  description = "Registry ID (AWS account ID) hosting the repository."
  value       = aws_ecr_repository.this.registry_id
}
