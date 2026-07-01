# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/postgresql.hcl — shared inputs for the RDS PostgreSQL stack (Component 5).
# PostgreSQL 16, 100GB gp3 autoscale to 1TB, 7-day backups + PITR, KMS encryption, private subnet group.
# Parameter group bumps are LOCKED by spec. Instance class + multi_az vary by env (env.hcl).
# This stack provisions the SERVER; modules/postgres-bootstrap (Component 16) initialises databases/users.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//postgresql"
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    vpc_id             = "vpc-00000000000000000"
    private_subnet_ids = ["subnet-aaaa", "subnet-bbbb", "subnet-cccc"]
    eks_node_sg_id     = "sg-00000000000000000"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  identifier = "cypherx-${local.env}"

  engine         = "postgres"
  engine_version = "16" # Component 5: PostgreSQL 16.

  # Component 5: 100GB gp3, autoscale to 1TB.
  allocated_storage     = 100
  max_allocated_storage = 1000
  storage_type          = "gp3"
  storage_encrypted     = true # KMS (Component 5).

  # Component 5: 7-day retention, daily automated, PITR.
  backup_retention_period = 7
  backup_window           = "03:00-04:00"

  db_subnet_group_subnet_ids = dependency.vpc.outputs.private_subnet_ids
  vpc_id                     = dependency.vpc.outputs.vpc_id

  # Component 3 sg-rds: inbound 5432 from sg-eks-nodes only.
  allowed_security_group_ids = [dependency.vpc.outputs.eks_node_sg_id]

  port = 5432

  # Master credentials sourced from Doppler/SSM at apply time — NEVER hardcoded.
  # The module reads them from var.master_username / var.master_password (documented as Doppler-sourced).
  manage_master_user_password = true # use AWS-managed master secret (Secrets Manager) — no plaintext pw in state.

  # Component 5 parameter group — values LOCKED. pgvector is NOT preloaded (it loads as a regular extension).
  parameter_group_family = "postgres16"
  db_parameters = [
    { name = "max_connections", value = "1000", apply_method = "pending-reboot" },
    { name = "shared_preload_libraries", value = "pg_stat_statements", apply_method = "pending-reboot" },
    { name = "log_min_duration_statement", value = "500", apply_method = "immediate" },
    { name = "idle_in_transaction_session_timeout", value = "60000", apply_method = "immediate" },
  ]
}
