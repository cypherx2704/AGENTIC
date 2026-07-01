###############################################################################
# MSK Kafka module — outputs
###############################################################################

output "cluster_arn" {
  description = "MSK cluster ARN."
  value       = aws_msk_cluster.this.arn
}

output "cluster_name" {
  description = "MSK cluster name (cypherx-<env>-kafka)."
  value       = aws_msk_cluster.this.cluster_name
}

output "bootstrap_brokers_tls" {
  description = "Bootstrap broker list (TLS, port 9094). For non-SASL TLS clients."
  value       = aws_msk_cluster.this.bootstrap_brokers_tls
}

output "bootstrap_brokers_sasl_scram" {
  description = "Bootstrap broker list (SASL/SCRAM over TLS, port 9096). Primary client endpoint for platform services."
  value       = aws_msk_cluster.this.bootstrap_brokers_sasl_scram
}

output "zookeeper_connect_string" {
  description = "ZooKeeper connect string (legacy; KRaft preferred where supported)."
  value       = aws_msk_cluster.this.zookeeper_connect_string
}

output "scram_secret_arn" {
  description = "Secrets Manager ARN holding the SASL/SCRAM credential (username/password from Doppler)."
  value       = aws_secretsmanager_secret.scram.arn
}

output "kms_key_arn" {
  description = "KMS key ARN used for at-rest encryption."
  value       = local.msk_kms_key_arn
}

output "configuration_arn" {
  description = "MSK configuration ARN (server.properties)."
  value       = aws_msk_configuration.this.arn
}
