# Component 3 — VPC & Networking.
#
# VPC 10.0.0.0/16 (us-east-1):
#   private 10.0.1-3.0/24 (3 AZs) — EKS nodes, RDS, MSK, ElastiCache
#   public  10.0.101-103.0/24     — ALB, NAT gateways only
#   3 NAT gateways (1 per AZ — HA), 1 IGW, per-AZ private route tables.
#
# Networking is delegated to the official terraform-aws-modules/vpc (~> 5).
# Security groups are raw resources (security_groups.tf) so the exact 6-SG
# ingress/egress graph in Component 3 is explicit and reviewable.

locals {
  name = "${var.name_prefix}-${var.env}"

  common_tags = merge(var.tags, {
    Environment = var.env
    ManagedBy   = "terraform"
    Module      = "vpc"
  })
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.8"

  name = local.name
  cidr = var.vpc_cidr

  azs             = var.azs
  private_subnets = var.private_subnet_cidrs
  public_subnets  = var.public_subnet_cidrs

  # One NAT gateway per AZ (3 total) for HA outbound. dev may set
  # single_nat_gateway = true to save cost.
  enable_nat_gateway     = true
  single_nat_gateway     = var.single_nat_gateway
  one_nat_gateway_per_az = !var.single_nat_gateway

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Subnet tags required by AWS Load Balancer Controller (Component 10) and EKS.
  public_subnet_tags = {
    "kubernetes.io/role/elb"                    = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"           = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }

  tags = local.common_tags
}
