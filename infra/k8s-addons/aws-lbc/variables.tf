variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be one of: dev, staging, prod."
  }
}

variable "chart_version" {
  description = "eks/aws-load-balancer-controller Helm chart version (pinned)."
  type        = string
  default     = "1.8.1"
}

variable "namespace" {
  description = "Namespace for the controller. kube-system per AWS guidance."
  type        = string
  default     = "kube-system"
}

variable "cluster_name" {
  description = "EKS cluster name (cypherx-<env>). Sourced from the eks stack output."
  type        = string
}

variable "region" {
  description = "AWS region (us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC ID the cluster runs in. Sourced from the vpc stack output."
  type        = string
}

variable "irsa_role_arn" {
  description = <<-EOT
    ARN of the AWSLoadBalancerControllerRole IRSA role (from modules/iam). The
    controller's ServiceAccount is annotated with this role; the role grants
    ec2:*, elasticloadbalancing:*, iam:CreateServiceLinkedRole (Component 10).
  EOT
  type        = string
}

variable "service_account_name" {
  description = "Name of the controller ServiceAccount (must match the IRSA trust)."
  type        = string
  default     = "aws-load-balancer-controller"
}
