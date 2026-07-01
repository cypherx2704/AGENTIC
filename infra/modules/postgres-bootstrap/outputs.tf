# ---------------------------------------------------------------------------------------------------------------------
# modules/postgres-bootstrap/outputs.tf — Component 16.
# Outputs are non-sensitive identifiers only. Passwords are NEVER output (they live in Doppler).
# ---------------------------------------------------------------------------------------------------------------------

output "database_name" {
  description = "The application database that was created."
  value       = postgresql_database.platform.name
}

output "schemas" {
  description = "Map of service-key => schema name created."
  value       = { for k, v in local.services : k => v.schema }
}

output "runtime_users" {
  description = "Map of service-key => runtime (least-priv) DB username."
  value       = { for k, v in local.services : k => v.runtime_user }
}

output "ddl_users" {
  description = "Map of service-key => DDL (Atlas migration) DB username."
  value       = { for k, v in local.services : k => v.ddl_user }
}

output "extensions" {
  description = "Extensions enabled in the platform database."
  value       = [postgresql_extension.vector.name, postgresql_extension.pg_stat_statements.name]
}
