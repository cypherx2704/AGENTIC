# ---------------------------------------------------------------------------------------------------------------------
# environments/prod/env.hcl — PROD sizing. Large, multi-AZ everywhere. Stack files are identical to dev/staging
# (same _envcommon fragments); only these sizes + the prod-only DNS bare aliases differ.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env        = "prod"
  aws_region = "us-east-1"
  account_id = "333333333333" # prod account — replace with real id.

  # ----- VPC (Component 3) — 3 AZs, NAT per AZ (HA). -----
  az_count           = 3
  single_nat_gateway = false

  # ----- EKS managed node groups (Component 4) — fixed prod sizes per spec (system 3, observability 2). -----
  eks_managed_node_groups = {
    system-nodes = {
      instance_types = ["t3.medium"]
      min_size       = 3
      max_size       = 3
      desired_size   = 3 # Component 4: 3 fixed.
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
      desired_size   = 2 # Component 4: 2 fixed.
      labels         = { "node-role" = "observability" }
    }
  }

  # ----- RDS (Component 5) — prod: db.r6g.xlarge, multi-AZ. -----
  rds_instance_class = "db.r6g.xlarge"
  rds_multi_az       = true

  # ----- Valkey (Component 5) — 3-node cluster, multi-AZ. -----
  valkey_node_type       = "cache.r6g.large"
  valkey_num_cache_nodes = 3
  valkey_multi_az        = true

  # ----- MSK (Component 5) — 3 brokers, kafka.m5.large. -----
  kafka_instance_type = "kafka.m5.large"

  # ----- IAM / GitHub OIDC (Component 1) — prod trusts main only. -----
  github_oidc_subjects = [
    "repo:cypherx-ai/*:ref:refs/heads/main",
  ]
}
