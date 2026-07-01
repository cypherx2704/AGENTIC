variable "environment" {
  description = "Environment name (dev | staging | prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "cluster_name" {
  description = "EKS cluster name (cypherx-<env>). Used by Karpenter for instance discovery and the EC2NodeClass subnet/SG selectors."
  type        = string
}

variable "cluster_endpoint" {
  description = "EKS cluster API endpoint. Passed to the Karpenter controller settings."
  type        = string
}

variable "namespace" {
  description = "Namespace for the Karpenter controller. Convention: kube-system (runs on the system managed NG that hosts Karpenter itself)."
  type        = string
  default     = "kube-system"
}

variable "chart_version" {
  description = "Karpenter OCI chart version. Component 17b: v1.x."
  type        = string
  default     = "1.0.6"
}

variable "controller_role_arn" {
  description = "IRSA role ARN for the Karpenter controller (ec2 RunInstances, etc.). Provisioned by the G3 IAM stack. No static keys."
  type        = string
}

variable "node_iam_role_name" {
  description = "IAM role name Karpenter-launched nodes assume (EKS node role, ECR-pull scoped — Component 1). Referenced by the EC2NodeClass."
  type        = string
}

variable "instance_profile_name" {
  description = "Instance profile wrapping node_iam_role_name. If empty, the EC2NodeClass uses `role` discovery instead of a pre-made profile."
  type        = string
  default     = ""
}

variable "discovery_tag_value" {
  description = "Value of the karpenter.sh/discovery tag on subnets + security groups Karpenter should use. Set by the VPC/EKS stacks to the cluster name."
  type        = string
  default     = ""
}

variable "ami_alias" {
  description = "EC2NodeClass amiSelectorTerms alias (e.g. al2023@latest for the EKS-optimized AL2023 AMI matching the cluster K8s version 1.30)."
  type        = string
  default     = "al2023@latest"
}

variable "node_volume_size" {
  description = "Root EBS volume size for Karpenter-launched nodes (gp3)."
  type        = string
  default     = "100Gi"
}

variable "extra_values" {
  description = "Additional raw YAML values merged last into the Karpenter controller release."
  type        = string
  default     = ""
}
