# ---------------------------------------------------------------------------------------------------------------------
# modules/kafka-topics/main.tf — Component 17 (Kafka Topic Bootstrap).
#
# Declarative, idempotent, drift-detected creation of the Contract 5 core topics on the MSK cluster, their common
# config (min.insync.replicas=2, unclean.leader.election=false, compression=lz4), and the paired DLQ topics for
# every NON-compact topic. Compact auth.agent.* topics do NOT get a DLQ (Component 17).
#
# >>> COMPACT-TOPIC KEY RULE (do NOT change): producers of cypherx.auth.agent.* MUST set the Kafka message key to
#     agent_id (NOT tenant_id). A tenant_id-keyed compact topic collapses to one record per tenant and loses every
#     prior agent state. This module configures the topics; the key is set producer-side. See README + topics.md.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  # ---------------------------------------------------------------------------------------------------------------
  # Convenient retention constants (ms). "infinite" => -1 per Kafka semantics.
  # ---------------------------------------------------------------------------------------------------------------
  ms_per_day = 86400000

  ret_infinite = -1
  ret_30d      = 30 * local.ms_per_day  # 2592000000
  ret_90d      = 90 * local.ms_per_day  # 7776000000
  ret_365d     = 365 * local.ms_per_day # 31536000000

  # ---------------------------------------------------------------------------------------------------------------
  # Component 17 core topics — EXACT partitions / cleanup.policy / retention. replication = var.default_replication_factor (3).
  #   compact  -> auth.agent.*  (infinite retention, keyed by agent_id producer-side, NO DLQ)
  #   delete   -> everything else (gets a paired .dlq)
  # ---------------------------------------------------------------------------------------------------------------
  core_topics = {
    "cypherx.auth.agent.registered" = {
      partitions     = 6
      cleanup_policy = "compact"
      retention_ms   = local.ret_infinite
    }
    "cypherx.auth.agent.deactivated" = {
      partitions     = 6
      cleanup_policy = "compact"
      retention_ms   = local.ret_infinite
    }
    "cypherx.llms.request.completed" = {
      partitions     = 12
      cleanup_policy = "delete"
      retention_ms   = local.ret_90d
    }
    "cypherx.llms.budget.alert" = {
      partitions     = 3
      cleanup_policy = "delete"
      retention_ms   = local.ret_30d
    }
    "cypherx.guardrails.violation.detected" = {
      partitions     = 12
      cleanup_policy = "delete"
      retention_ms   = local.ret_90d
    }
    "cypherx.agent.task.submitted" = {
      partitions     = 24
      cleanup_policy = "delete"
      retention_ms   = local.ret_30d
    }
    "cypherx.agent.task.completed" = {
      partitions     = 24
      cleanup_policy = "delete"
      retention_ms   = local.ret_30d
    }
    "cypherx.agent.task.failed" = {
      partitions     = 24
      cleanup_policy = "delete"
      retention_ms   = local.ret_30d
    }
    "cypherx.platform.audit.event" = {
      partitions     = 12
      cleanup_policy = "delete"
      retention_ms   = local.ret_365d
    }
    "cypherx.billing.usage.recorded" = {
      partitions     = 6
      cleanup_policy = "delete"
      retention_ms   = local.ret_365d
    }
  }

  # ---------------------------------------------------------------------------------------------------------------
  # DLQ topics — created alongside each NON-compact topic (Contract 5). Same partitions as the original,
  # replication 3, cleanup.policy=delete, retention 30 days. Compact topics are excluded (Component 17).
  # ---------------------------------------------------------------------------------------------------------------
  dlq_topics = {
    for name, cfg in local.core_topics : "${name}.dlq" => {
      partitions     = cfg.partitions
      cleanup_policy = "delete"
      retention_ms   = var.dlq_retention_ms
    }
    if cfg.cleanup_policy != "compact"
  }

  # All topics this module manages.
  all_topics = merge(local.core_topics, local.dlq_topics)

  # ---------------------------------------------------------------------------------------------------------------
  # Common config applied to EVERY topic (Component 17):
  #   min.insync.replicas = 2            (writes require >= 2 ISR acks)
  #   unclean.leader.election.enable=false (no data loss on broker failure)
  #   compression.type = lz4
  # ---------------------------------------------------------------------------------------------------------------
  common_config = {
    "min.insync.replicas"            = "2"
    "unclean.leader.election.enable" = "false"
    "compression.type"               = "lz4"
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# PROVIDER — MSK over SASL_SSL with SCRAM-SHA-512. Creds from Doppler (TF_VAR_kafka_sasl_*). NEVER hardcoded.
# ---------------------------------------------------------------------------------------------------------------------
provider "kafka" {
  bootstrap_servers = var.bootstrap_servers

  tls_enabled    = var.tls_enabled
  sasl_mechanism = var.sasl_mechanism
  sasl_username  = var.sasl_username
  sasl_password  = var.sasl_password

  # MSK presents a public-CA / ACM-PCA chain; the system trust store validates it. Do not skip verification.
  skip_tls_verify = false
}

# ---------------------------------------------------------------------------------------------------------------------
# TOPICS — core + DLQ in one declarative for_each. Per-topic config = common_config + cleanup.policy + retention.ms.
# ---------------------------------------------------------------------------------------------------------------------
resource "kafka_topic" "this" {
  for_each = local.all_topics

  name               = each.key
  partitions         = each.value.partitions
  replication_factor = var.default_replication_factor

  config = merge(
    local.common_config,
    {
      "cleanup.policy" = each.value.cleanup_policy
      "retention.ms"   = tostring(each.value.retention_ms)
    }
  )
}
