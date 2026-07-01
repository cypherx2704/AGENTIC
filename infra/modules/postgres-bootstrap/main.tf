# ---------------------------------------------------------------------------------------------------------------------
# modules/postgres-bootstrap/main.tf — Component 16 (Database Initialisation).
#
# Terraform-owned, runs ONCE per environment, idempotent. Owns: database, schemas, per-service runtime users
# (*_user, least-priv), per-service DDL users (*_ddl, CREATE+USAGE+CREATEROLE), pgvector + pg_stat_statements
# extensions, and the ALTER DEFAULT PRIVILEGES grants so Atlas-created tables/sequences are usable by runtime users.
#
# Atlas (per service, K8s Job) owns tables/columns/indexes/RLS WITHIN each schema — NOT this module. See Contract 14.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  # Component 16: the 7 service schemas and their user naming. The map key is the Doppler service key used to look
  # up runtime_passwords[key] / ddl_passwords[key]. Component 14 fixes the *_user / *_ddl runtime names.
  #
  # service key -> { schema, runtime_user, ddl_user }
  services = {
    auth       = { schema = "auth", runtime_user = "auth_user", ddl_user = "auth_ddl" }
    llms       = { schema = "llms", runtime_user = "llms_user", ddl_user = "llms_ddl" }
    guardrails = { schema = "guardrails", runtime_user = "grd_user", ddl_user = "grd_ddl" }
    memory     = { schema = "memory", runtime_user = "mem_user", ddl_user = "mem_ddl" }
    rag        = { schema = "rag", runtime_user = "rag_user", ddl_user = "rag_ddl" }
    xagent     = { schema = "xagent", runtime_user = "xagent_user", ddl_user = "xagent_ddl" }
    # Map key is the canonical Doppler SERVICE name (Contract 20: "platform-mgmt"), used to look up
    # runtime_passwords["platform-mgmt"] -> db/platform-mgmt/{runtime,ddl}_password. The SCHEMA stays
    # "platform" and the user stays plat_user. This is the only service whose Doppler name != schema name;
    # keying by the schema ("platform") would look up the non-existent db/platform/* path and fail apply.
    "platform-mgmt" = { schema = "platform", runtime_user = "plat_user", ddl_user = "plat_ddl" }
    # CoreProjects cypherx-a1 (Autonomous Engineering Memory) — a consuming app with its own
    # schema + roles. Doppler paths db/cypherx-a1/{runtime,ddl}_password must exist for apply.
    "cypherx-a1"    = { schema = "cypherx_a1", runtime_user = "cxa1_user", ddl_user = "cxa1_ddl" }
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# Resolve the bootstrap superuser password: prefer the explicit TF_VAR (Doppler), else the AWS-managed master secret.
# ---------------------------------------------------------------------------------------------------------------------
data "aws_secretsmanager_secret_version" "master" {
  count     = var.master_user_secret_arn != "" && var.pg_superuser_password == "" ? 1 : 0
  secret_id = var.master_user_secret_arn
}

locals {
  superuser_password = (
    var.pg_superuser_password != ""
    ? var.pg_superuser_password
    : (
      var.master_user_secret_arn != ""
      ? jsondecode(data.aws_secretsmanager_secret_version.master[0].secret_string)["password"]
      : ""
    )
  )
}

# ---------------------------------------------------------------------------------------------------------------------
# PROVIDER — connect as the RDS master to the default "postgres" maintenance DB to create the app database.
# A SECOND provider alias (below) connects to cypherx_platform itself to create schemas/extensions/grants, because
# CREATE SCHEMA / CREATE EXTENSION must run inside the target database.
# ---------------------------------------------------------------------------------------------------------------------
provider "postgresql" {
  host            = var.pg_host
  port            = var.pg_port
  username        = var.pg_superuser
  password        = local.superuser_password
  sslmode         = var.sslmode
  superuser       = false # RDS master is NOT a true superuser (rds_superuser); tell the provider so.
  connect_timeout = 15
  database        = "postgres"
}

provider "postgresql" {
  alias           = "app"
  host            = var.pg_host
  port            = var.pg_port
  username        = var.pg_superuser
  password        = local.superuser_password
  sslmode         = var.sslmode
  superuser       = false
  connect_timeout = 15
  database        = var.database_name
}

# ---------------------------------------------------------------------------------------------------------------------
# CREATE DATABASE cypherx_platform;
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_database" "platform" {
  name              = var.database_name
  owner             = var.pg_superuser
  encoding          = "UTF8"
  lc_collate        = "en_US.UTF-8"
  lc_ctype          = "en_US.UTF-8"
  connection_limit  = -1
  allow_connections = true
}

# ---------------------------------------------------------------------------------------------------------------------
# CREATE EXTENSION IF NOT EXISTS vector;  (pgvector — regular extension, NOT preloaded — Component 5 note)
# CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  (preloaded via shared_preload_libraries in the param group)
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_extension" "vector" {
  provider = postgresql.app
  name     = "vector"
  database = postgresql_database.platform.name
}

resource "postgresql_extension" "pg_stat_statements" {
  provider = postgresql.app
  name     = "pg_stat_statements"
  database = postgresql_database.platform.name
}

# ---------------------------------------------------------------------------------------------------------------------
# RUNTIME USERS (*_user) — least privilege. No CREATE on schema, no CREATEROLE. Password from Doppler.
# Component 16: GRANT USAGE ON SCHEMA + ALTER DEFAULT PRIVILEGES for future Atlas-created tables/sequences.
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_role" "runtime" {
  for_each = local.services

  name     = each.value.runtime_user
  login    = true
  password = var.runtime_passwords[each.key]

  # Least privilege — explicitly deny the dangerous attributes.
  superuser       = false
  create_role     = false
  create_database = false
  inherit         = true

  # Keep the password out of CREATE ROLE logs where the provider supports it.
  encrypted_password = true
}

# ---------------------------------------------------------------------------------------------------------------------
# DDL USERS (*_ddl) — used only by Atlas migration Jobs. CREATE + USAGE on the schema + CREATEROLE (so Atlas can
# create the per-schema RLS role). Password from Doppler db/<svc>/ddl_password.
#
# KNOWN LIMITATION (see README): CREATEROLE is CLUSTER-WIDE, not schema-scoped — Postgres lacks the primitive.
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_role" "ddl" {
  for_each = local.services

  name     = each.value.ddl_user
  login    = true
  password = var.ddl_passwords[each.key]

  superuser       = false
  create_database = false
  inherit         = true

  # Component 16: GRANT CREATEROLE TO <svc>_ddl — cluster-wide (Postgres limitation, documented in README).
  create_role = true

  encrypted_password = true
}

# ---------------------------------------------------------------------------------------------------------------------
# SCHEMAS — one per service, owned by that service's DDL user so Atlas (running as *_ddl) can fully manage it.
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_schema" "service" {
  for_each = local.services
  provider = postgresql.app

  name     = each.value.schema
  database = postgresql_database.platform.name
  owner    = postgresql_role.ddl[each.key].name

  # Component 16 grants live in postgresql_grant resources below (explicit, auditable).
  depends_on = [postgresql_extension.vector, postgresql_extension.pg_stat_statements]
}

# ---------------------------------------------------------------------------------------------------------------------
# GRANTS — DDL user: CREATE, USAGE on its own schema (Component 16: GRANT CREATE, USAGE ON SCHEMA auth TO auth_ddl).
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_grant" "ddl_schema" {
  for_each = local.services
  provider = postgresql.app

  database    = postgresql_database.platform.name
  role        = postgresql_role.ddl[each.key].name
  schema      = postgresql_schema.service[each.key].name
  object_type = "schema"
  privileges  = ["CREATE", "USAGE"]
}

# ---------------------------------------------------------------------------------------------------------------------
# GRANTS — runtime user: USAGE on its own schema only (Component 16: GRANT USAGE ON SCHEMA auth TO auth_user).
# No CREATE — runtime users never run DDL.
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_grant" "runtime_schema" {
  for_each = local.services
  provider = postgresql.app

  database    = postgresql_database.platform.name
  role        = postgresql_role.runtime[each.key].name
  schema      = postgresql_schema.service[each.key].name
  object_type = "schema"
  privileges  = ["USAGE"]
}

# ---------------------------------------------------------------------------------------------------------------------
# DEFAULT PRIVILEGES — Atlas (as *_ddl) creates tables/sequences; pre-grant the runtime user DML on FUTURE objects.
# Component 16:
#   ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO auth_user;
#   ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT USAGE,SELECT ON SEQUENCES TO auth_user;
#
# `owner` is the *_ddl user (the object creator), so the default privileges apply to objects Atlas creates.
# ---------------------------------------------------------------------------------------------------------------------
resource "postgresql_default_privileges" "runtime_tables" {
  for_each = local.services
  provider = postgresql.app

  database    = postgresql_database.platform.name
  role        = postgresql_role.runtime[each.key].name
  owner       = postgresql_role.ddl[each.key].name
  schema      = postgresql_schema.service[each.key].name
  object_type = "table"
  privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
}

resource "postgresql_default_privileges" "runtime_sequences" {
  for_each = local.services
  provider = postgresql.app

  database    = postgresql_database.platform.name
  role        = postgresql_role.runtime[each.key].name
  owner       = postgresql_role.ddl[each.key].name
  schema      = postgresql_schema.service[each.key].name
  object_type = "sequence"
  privileges  = ["USAGE", "SELECT"]
}
