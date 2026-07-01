###############################################################################
# RDS PostgreSQL module — outputs
###############################################################################

output "db_instance_identifier" {
  description = "RDS instance identifier (cypherx-<env>-postgres)."
  value       = module.db.db_instance_identifier
}

output "endpoint" {
  description = "Connection endpoint (host:port). Consumed by PgBouncer config + service DSNs."
  value       = module.db.db_instance_endpoint
}

output "address" {
  description = "DB host (without port)."
  value       = module.db.db_instance_address
}

output "port" {
  description = "DB port."
  value       = module.db.db_instance_port
}

output "database_name" {
  description = "Initial database name (cypherx_platform)."
  value       = var.database_name
}

output "master_username" {
  description = "Master DB username."
  value       = var.master_username
}

output "db_subnet_group_name" {
  description = "DB subnet group name (private subnets only)."
  value       = module.db.db_subnet_group_id
}

output "parameter_group_name" {
  description = "Parameter group name with the Component 5 settings."
  value       = module.db.db_parameter_group_id
}

output "kms_key_arn" {
  description = "KMS key ARN used for storage + Performance Insights encryption."
  value       = local.rds_kms_key_arn
}
