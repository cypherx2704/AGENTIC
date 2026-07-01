###############################################################################
# ElastiCache Valkey — Component 5 (phase-01-infrastructure.md, lines 282-290)
#
# Raw resources (not the wrapper module) for precise control of the Valkey engine,
# TLS in-transit, AUTH token, and at-rest KMS encryption.
#
# Locked-in (Component 5):
#   engine                     = valkey 7.x
#   3 nodes (prod) / 1 node (dev)
#   cache.r6g.large (prod) / cache.t3.micro (dev)
#   multi-AZ enabled (prod)
#   transit_encryption_enabled = true     (TLS on)
#   auth_token                 = from Doppler (sensitive variable)
#   at_rest_encryption_enabled = true     (KMS)
###############################################################################

locals {
  name = coalesce(var.name, "cypherx-${var.env}-valkey")

  # Multi-AZ + automatic failover require >= 2 nodes (1 primary + >=1 replica).
  automatic_failover = var.node_count > 1
  multi_az           = var.multi_az_enabled && local.automatic_failover

  # t3.micro does not support snapshots; disable retention there.
  snapshot_retention = can(regex("^cache\\.t3\\.micro$", var.node_type)) ? 0 : var.snapshot_retention_days

  base_tags = merge(
    {
      "Environment" = var.env
      "ManagedBy"   = "terraform"
      "Component"   = "valkey"
    },
    var.tags,
  )
}

###############################################################################
# Optional dedicated KMS key for at-rest encryption.
###############################################################################

resource "aws_kms_key" "valkey" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "ElastiCache Valkey at-rest encryption for ${local.name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.base_tags
}

resource "aws_kms_alias" "valkey" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.valkey[0].key_id
}

locals {
  valkey_kms_key_arn = var.kms_key_arn != null ? var.kms_key_arn : aws_kms_key.valkey[0].arn
}

###############################################################################
# Subnet group (private subnets) + parameter group.
###############################################################################

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.name}-subnets"
  subnet_ids = var.private_subnet_ids
  tags       = local.base_tags
}

resource "aws_elasticache_parameter_group" "this" {
  name        = "${local.name}-params"
  family      = "valkey7"
  description = "Valkey 7 parameter group for ${local.name}"
  tags        = local.base_tags
}

###############################################################################
# Replication group: Valkey 7.x, TLS in-transit, AUTH token, KMS at-rest.
###############################################################################

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.name
  description          = "CypherX Valkey (${var.env})"

  engine         = "valkey"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = var.port

  # 1 primary + (node_count - 1) replicas. node_count=1 -> single node, no failover.
  num_cache_clusters = var.node_count

  automatic_failover_enabled = local.automatic_failover
  multi_az_enabled           = local.multi_az

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = var.security_group_ids
  parameter_group_name = aws_elasticache_parameter_group.this.name

  # --- TLS in-transit + AUTH token (Component 5) -----------------------------
  transit_encryption_enabled = true
  auth_token                 = var.auth_token
  auth_token_update_strategy = "ROTATE"

  # --- KMS at-rest (Component 5) ---------------------------------------------
  at_rest_encryption_enabled = true
  kms_key_id                 = local.valkey_kms_key_arn

  # --- Snapshots / maintenance -----------------------------------------------
  snapshot_retention_limit = local.snapshot_retention
  snapshot_window          = local.snapshot_retention > 0 ? var.snapshot_window : null
  maintenance_window       = var.maintenance_window

  apply_immediately = false

  tags = local.base_tags

  lifecycle {
    ignore_changes = [auth_token]
  }
}
