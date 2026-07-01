variable "env" {
  description = "Environment name (dev | staging | prod)."
  type        = string
}

variable "region" {
  description = "AWS region. Component 3 specifies us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for resource names. VPC is named <prefix>-<env>."
  type        = string
  default     = "cypherx"
}

variable "vpc_cidr" {
  description = "VPC CIDR block (Component 3: 10.0.0.0/16)."
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Availability zones. Component 3 uses us-east-1a/b/c (3 AZs)."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs (EKS nodes, RDS, MSK, ElastiCache). Component 3: 10.0.1-3.0/24."
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDRs (ALB, NAT gateways only). Component 3: 10.0.101-103.0/24."
  type        = list(string)
  default     = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]
}

variable "single_nat_gateway" {
  description = "When true, deploy ONE shared NAT gateway (dev cost-saving). Prod MUST be false — Component 3 mandates one NAT GW per AZ (3 total) for HA."
  type        = bool
  default     = false
}

variable "cluster_name" {
  description = "EKS cluster name for subnet kubernetes.io/cluster tagging and ELB role tags. Typically <prefix>-<env>."
  type        = string
}

variable "tags" {
  description = "Tags applied to all networking resources."
  type        = map(string)
  default     = {}
}
