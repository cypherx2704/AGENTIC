# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/postgres-bootstrap.hcl — shared inputs for the DB initialisation stack (Component 16).
# Uses the hashicorp/postgresql provider to CREATE DATABASE cypherx_platform, extensions, the 7 schemas, and the
# per-service runtime (*_user) + DDL (*_ddl) users. Runs ONCE per environment. Idempotent.
#
# Passwords come from Doppler at db/<svc>/runtime_password and db/<svc>/ddl_password — sourced into TF_VAR_* by the
# wrapper (or via the doppler provider data source) — NEVER hardcoded.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//postgres-bootstrap"
}

# This stack connects to the RDS instance the postgresql stack created. We read its endpoint from that state.
dependency "postgresql" {
  config_path = "../postgresql"

  mock_outputs = {
    db_instance_address    = "cypherx-mock.xxxx.us-east-1.rds.amazonaws.com"
    db_instance_port       = 5432
    master_username        = "cypherx_admin"
    master_user_secret_arn = "arn:aws:secretsmanager:us-east-1:000000000000:secret:rds!mock"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  env = local.env

  pg_host = dependency.postgresql.outputs.db_instance_address
  pg_port = dependency.postgresql.outputs.db_instance_port

  # Superuser/admin connection — the RDS master. Username from postgresql outputs; password is read by the module
  # from the AWS-managed master secret (master_user_secret_arn) or TF_VAR_pg_superuser_password (Doppler-sourced).
  pg_superuser           = dependency.postgresql.outputs.master_username
  master_user_secret_arn = dependency.postgresql.outputs.master_user_secret_arn

  database_name = "cypherx_platform"

  # Runtime + DDL passwords are NOT set here. They are injected as TF_VAR_<svc>_runtime_password /
  # TF_VAR_<svc>_ddl_password from Doppler (db/<svc>/runtime_password, db/<svc>/ddl_password). The module
  # declares them as sensitive variables with no default so a missing value fails the apply loudly.
}
