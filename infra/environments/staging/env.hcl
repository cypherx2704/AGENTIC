# ---------------------------------------------------------------------------------------------------------------------
# environments/staging/env.hcl — STAGING sizing. Mid-point between dev and prod: multi-AZ on, prod-like topology,
# slightly smaller instances. Stack files are identical to dev (they include the same _envcommon fragments).
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env        = "staging"
  aws_region = "us-east-1"
  account_id = "222222222222" # staging account — replace with real id.

  # ----- VPC (Component 3) — 3 AZs, NAT per AZ (prod-like). -----
  az_count           = 3
  single_nat_gateway = false

  # ----- EKS managed node groups (Component 4) — prod-like fixed sizes. -----
  eks_managed_node_groups = {
    system-nodes = {
      instance_types = ["t3.medium"]
      min_size       = 3
      max_size       = 3
      desired_size   = 3
      labels         = { "node-role" = "system" }
      taints         = [{
        key    = "CriticalAddonsOnly"
        value  = "true"
        effect = "NO_SCHEDULE"
      }]
    }
    observability = {
      instance_types = ["m5.large"]
      min_size       = 2
      max_size       = 2
      desired_size   = 2
      labels         = { "node-role" = "observability" }
    }
  }

  # ----- RDS (Component 5) — multi-AZ on; mid instance. -----
  rds_instance_class = "db.r6g.large"
  rds_multi_az       = true

  # ----- Valkey (Component 5) — multi-node, multi-AZ. -----
  valkey_node_type       = "cache.r6g.large"
  valkey_num_cache_nodes = 3
  valkey_multi_az        = true

  # ----- MSK (Component 5) — 3 brokers, mid instance. -----
  kafka_instance_type = "kafka.m5.large"

  # ----- IAM / GitHub OIDC (Component 1) -----
  github_oidc_subjects = [
    "repo:cypherx-ai/*:ref:refs/heads/main",
  ]
}
