###############################################################################
# ElastiCache Valkey module — outputs
###############################################################################

output "replication_group_id" {
  description = "Valkey replication group ID."
  value       = aws_elasticache_replication_group.this.replication_group_id
}

output "primary_endpoint_address" {
  description = "Primary endpoint hostname (writes). Multi-node only."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "reader_endpoint_address" {
  description = "Reader endpoint hostname (read replicas). Multi-node only."
  value       = aws_elasticache_replication_group.this.reader_endpoint_address
}

output "configuration_endpoint_address" {
  description = "Configuration endpoint (cluster-mode). Null for non-cluster-mode replication groups."
  value       = aws_elasticache_replication_group.this.configuration_endpoint_address
}

output "port" {
  description = "Valkey port."
  value       = var.port
}

output "tls_enabled" {
  description = "Whether transit encryption (TLS) is enabled. Always true."
  value       = aws_elasticache_replication_group.this.transit_encryption_enabled
}

output "kms_key_arn" {
  description = "KMS key ARN used for at-rest encryption."
  value       = local.valkey_kms_key_arn
}
