# ---------------------------------------------------------------------------------------------------------------------
# environments/dev/env.hcl — DEV environment sizing (small, single-AZ where the spec allows).
# Read by the root terragrunt.hcl and by every _envcommon fragment. This is the ONLY place dev sizes live.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env        = "dev"
  aws_region = "us-east-1"

  # Per-env AWS account (Component 4: separate accounts/VPCs per env). Replace with the real dev account id.
  account_id = "111111111111"

  # ----- VPC (Component 3) -----
  # dev runs across 2 AZs to cut NAT/EBS cost; prod uses 3. CIDRs are fixed in _envcommon/vpc.hcl.
  az_count           = 2
  single_nat_gateway = true # dev: one NAT (not one-per-AZ) — cost.

  # ----- EKS managed node groups (Component 4) — fixed, ON_DEMAND. core/agent/tools are Karpenter, NOT here. -----
  eks_managed_node_groups = {
    system-nodes = {
      instance_types = ["t3.medium"]
      min_size       = 2
      max_size       = 3
      desired_size   = 2 # dev: 2 (prod: 3 fixed)
      labels         = { "node-role" = "system" }
      taints         = [{
        key    = "CriticalAddonsOnly"
        value  = "true"
        effect = "NO_SCHEDULE"
      }]
    }
    observability = {
      instance_types = ["m5.large"]
      min_size       = 1
      max_size       = 2
      desired_size   = 1 # dev: 1 (prod: 2 fixed)
      labels         = { "node-role" = "observability" }
    }
  }

  # ----- RDS PostgreSQL (Component 5) — dev: small, single-AZ -----
  rds_instance_class = "db.t3.medium"
  rds_multi_az       = false

  # ----- Valkey / ElastiCache (Component 5) — dev: single node -----
  valkey_node_type       = "cache.t3.micro"
  valkey_num_cache_nodes = 1
  valkey_multi_az        = false

  # ----- MSK Kafka (Component 5) — 3 brokers always; dev uses smaller instance -----
  kafka_instance_type = "kafka.t3.small"

  # ----- IAM / GitHub OIDC (Component 1) -----
  github_oidc_subjects = [
    "repo:cypherx-ai/*:ref:refs/heads/main",
    "repo:cypherx-ai/*:pull_request",
  ]
}
