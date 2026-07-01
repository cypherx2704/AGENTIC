# ---------------------------------------------------------------------------------------------------------------------
# modules/kafka-topics/outputs.tf — Component 17.
# ---------------------------------------------------------------------------------------------------------------------

output "core_topic_names" {
  description = "The Component 17 core topics created (excludes DLQs)."
  value       = sort(keys(local.core_topics))
}

output "dlq_topic_names" {
  description = "The paired DLQ topics created (one per non-compact core topic)."
  value       = sort(keys(local.dlq_topics))
}

output "compact_topics" {
  description = "Compact topics — producers MUST key these by agent_id (NOT tenant_id). See README."
  value       = sort([for n, c in local.core_topics : n if c.cleanup_policy == "compact"])
}

output "all_topic_names" {
  description = "Every topic managed by this module (core + DLQ)."
  value       = sort(keys(local.all_topics))
}
