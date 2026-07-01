###############################################################################
# RDS PostgreSQL — Component 5 (phase-01-infrastructure.md, lines 256-270)
#
# Wraps terraform-aws-modules/rds/aws ~> 6.
#
# Parameter group (locked-in, Component 5):
#   max_connections                     = 1000  (was 500)
#   shared_preload_libraries            = pg_stat_statements
#       -> pgvector is a `CREATE EXTENSION vector`, NOT a preload. Do NOT add
#          `vector` to shared_preload_libraries; it is loaded by the
#          postgres-bootstrap stack via CREATE EXTENSION.
#   log_min_duration_statement          = 500   (log slow queries >= 500ms)
#   idle_in_transaction_session_timeout = 60000 (kill idle txns after 60s —
#                                                prevents RLS context leak,
#                                                Contract 13 SET LOCAL hygiene)
###############################################################################

locals {
  identifier = coalesce(var.identifier, "cypherx-${var.env}-postgres")
  family     = "postgres${var.engine_version}"

  base_tags = merge(
    {
      "Environment" = var.env
      "ManagedBy"   = "terraform"
      "Component"   = "postgresql"
    },
    var.tags,
  )

  # Component 5 parameter set. Note shared_preload_libraries = pg_stat_statements ONLY.
  core_parameters = [
    {
      name         = "max_connections"
      value        = tostring(var.max_connections)
      apply_method = "pending-reboot"
    },
    {
      name         = "shared_preload_libraries"
      value        = "pg_stat_statements"
      apply_method = "pending-reboot"
    },
    {
      name         = "log_min_duration_statement"
      value        = tostring(var.log_min_duration_statement_ms)
      apply_method = "immediate"
    },
    {
      name         = "idle_in_transaction_session_timeout"
      value        = tostring(var.idle_in_transaction_session_timeout_ms)
      apply_method = "immediate"
    },
  ]

  parameters = concat(local.core_parameters, var.extra_parameters)
}

###############################################################################
# Optional dedicated KMS key for storage + Performance Insights encryption.
###############################################################################

resource "aws_kms_key" "rds" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "RDS PostgreSQL encryption for ${local.identifier}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.base_tags
}

resource "aws_kms_alias" "rds" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = "alias/${local.identifier}"
  target_key_id = aws_kms_key.rds[0].key_id
}

locals {
  rds_kms_key_arn = var.kms_key_arn != null ? var.kms_key_arn : aws_kms_key.rds[0].arn
}

###############################################################################
# RDS instance (db subnet group + parameter group created by the module).
###############################################################################

module "db" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.0"

  identifier = local.identifier

  # --- Engine (PostgreSQL 16) ------------------------------------------------
  engine               = "postgres"
  engine_version       = var.engine_version
  family               = local.family
  major_engine_version = var.engine_version
  instance_class       = var.instance_class

  # --- Storage (100GB gp3 -> autoscale 1TB) ----------------------------------
  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = var.storage_type
  storage_encrypted     = true
  kms_key_id            = local.rds_kms_key_arn

  # --- Credentials (sourced from Doppler/SSM) --------------------------------
  db_name                     = var.database_name
  username                    = var.master_username
  password                    = var.master_password
  manage_master_user_password = false
  port                        = var.port

  # --- Networking: private subnets only --------------------------------------
  multi_az               = var.multi_az
  create_db_subnet_group = true
  subnet_ids             = var.private_subnet_ids
  vpc_security_group_ids = var.security_group_ids
  publicly_accessible    = false

  # --- Backups / PITR (7-day retention) --------------------------------------
  backup_retention_period   = var.backup_retention_days
  backup_window             = var.backup_window
  maintenance_window        = var.maintenance_window
  copy_tags_to_snapshot     = true
  delete_automated_backups  = false
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.identifier}-final"

  # --- Parameter group (Component 5 settings) --------------------------------
  create_db_parameter_group = true
  parameter_group_name      = "${local.identifier}-pg16"
  parameters                = local.parameters

  # --- Observability ---------------------------------------------------------
  performance_insights_enabled          = var.performance_insights_enabled
  performance_insights_kms_key_id       = var.performance_insights_enabled ? local.rds_kms_key_arn : null
  performance_insights_retention_period = var.performance_insights_enabled ? 7 : null
  create_monitoring_role                = true
  monitoring_interval                   = 60
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  deletion_protection = var.deletion_protection
  apply_immediately   = false

  tags = local.base_tags
}
