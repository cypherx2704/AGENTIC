###############################################################################
# MSK Kafka — Component 5 (phase-01-infrastructure.md, lines 292-301)
#
# Raw aws_msk_cluster + SASL/SCRAM secret association for precise control of
# TLS in-transit, KMS at-rest, and SCRAM-SHA-512 auth.
#
# Locked-in (Component 5):
#   3 brokers (one per AZ)
#   kafka.m5.large (prod) / kafka.t3.small (dev)
#   100GB gp3 per broker
#   Kafka 3.6.x
#   encryption_in_transit = TLS
#   encryption_at_rest    = KMS
#   SASL SCRAM-SHA-512
#
# SCRAM secret distribution: the SASL credential is stored in a Secrets Manager
# secret encrypted with a CUSTOMER-managed KMS key (MSK forbids associating the
# default aws/secretsmanager key). The secret password is sourced from Doppler via
# the scram_password variable — never hardcoded.
###############################################################################

locals {
  name = coalesce(var.name, "cypherx-${var.env}-kafka")

  base_tags = merge(
    {
      "Environment" = var.env
      "ManagedBy"   = "terraform"
      "Component"   = "kafka"
    },
    var.tags,
  )

  # Cluster-level server.properties. Topic-level config (partitions, cleanup.policy,
  # retention, DLQ pairings) is owned by the kafka-topics stack (Component 17),
  # not this module.
  server_properties = <<-PROPERTIES
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=${var.min_insync_replicas}
    num.partitions=3
    unclean.leader.election.enable=false
    compression.type=lz4
  PROPERTIES
}

###############################################################################
# KMS keys: one for MSK at-rest, one for the SCRAM Secrets Manager secret.
# (MSK requires a customer-managed key for the associated SCRAM secret.)
###############################################################################

resource "aws_kms_key" "msk" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "MSK at-rest encryption for ${local.name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.base_tags
}

resource "aws_kms_alias" "msk" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.msk[0].key_id
}

locals {
  msk_kms_key_arn = var.kms_key_arn != null ? var.kms_key_arn : aws_kms_key.msk[0].arn
}

resource "aws_kms_key" "scram" {
  description             = "MSK SCRAM secret encryption for ${local.name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.base_tags
}

resource "aws_kms_alias" "scram" {
  name          = "alias/${local.name}-scram"
  target_key_id = aws_kms_key.scram.key_id
}

###############################################################################
# Cluster configuration (server.properties).
###############################################################################

resource "aws_msk_configuration" "this" {
  name              = "${local.name}-config"
  kafka_versions    = [var.kafka_version]
  server_properties = local.server_properties

  lifecycle {
    create_before_destroy = true
  }
}

###############################################################################
# CloudWatch log group for broker logs.
###############################################################################

resource "aws_cloudwatch_log_group" "broker" {
  name              = "/aws/msk/${local.name}"
  retention_in_days = 90
  tags              = local.base_tags
}

###############################################################################
# MSK cluster: TLS in-transit, KMS at-rest, SASL/SCRAM.
###############################################################################

resource "aws_msk_cluster" "this" {
  cluster_name           = local.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.broker_count

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = var.private_subnet_ids
    security_groups = var.security_group_ids

    storage_info {
      ebs_storage_info {
        volume_size = var.broker_volume_gb
      }
    }
  }

  # --- Encryption: TLS in-transit + KMS at-rest (Component 5) -----------------
  encryption_info {
    encryption_at_rest_kms_key_arn = local.msk_kms_key_arn

    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  # --- SASL/SCRAM-SHA-512 auth (Component 5) ---------------------------------
  client_authentication {
    sasl {
      scram = true
    }
  }

  enhanced_monitoring = var.enhanced_monitoring

  open_monitoring {
    prometheus {
      jmx_exporter {
        enabled_in_broker = true
      }
      node_exporter {
        enabled_in_broker = true
      }
    }
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.broker.name
      }
    }
  }

  tags = local.base_tags
}

###############################################################################
# SASL/SCRAM credential in Secrets Manager, associated with the cluster.
# Password is sourced from Doppler via var.scram_password — never hardcoded.
###############################################################################

resource "aws_secretsmanager_secret" "scram" {
  # MSK requires the secret name to be prefixed with "AmazonMSK_".
  name       = "AmazonMSK_${local.name}_app"
  kms_key_id = aws_kms_key.scram.arn
  tags       = local.base_tags
}

resource "aws_secretsmanager_secret_version" "scram" {
  secret_id = aws_secretsmanager_secret.scram.id
  secret_string = jsonencode({
    username = var.scram_username
    password = var.scram_password
  })
}

resource "aws_msk_scram_secret_association" "this" {
  cluster_arn     = aws_msk_cluster.this.arn
  secret_arn_list = [aws_secretsmanager_secret.scram.arn]

  depends_on = [aws_secretsmanager_secret_version.scram]
}
