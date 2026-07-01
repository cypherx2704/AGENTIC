# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/vpc.hcl — shared inputs for the VPC stack (Component 3).
# CIDRs are LOCKED by the spec and identical across envs; only az_count varies (dev=2, staging/prod=3) via env.hcl.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//vpc"
}

inputs = {
  name = "cypherx-${local.env}"

  # Component 3: VPC 10.0.0.0/16, region us-east-1.
  cidr_block = "10.0.0.0/16"

  # us-east-1a/b/c. dev uses the first az_count of these.
  azs = ["us-east-1a", "us-east-1b", "us-east-1c"]

  # Private subnets (EKS nodes, RDS, MSK, ElastiCache).
  private_subnet_cidrs = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]

  # Public subnets (ALB, NAT Gateways only).
  public_subnet_cidrs = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  # Component 3: NAT Gateways = 1 per AZ (HA). dev collapses to single_nat_gateway (see env.hcl override).
  one_nat_gateway_per_az = true

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Tag subnets so AWS LBC (Component 10) + Karpenter can discover them.
  public_subnet_tags = {
    "kubernetes.io/role/elb"                      = "1"
    "kubernetes.io/cluster/cypherx-${local.env}"  = "shared"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"             = "1"
    "kubernetes.io/cluster/cypherx-${local.env}"  = "shared"
    "karpenter.sh/discovery"                      = "cypherx-${local.env}"
  }
}
