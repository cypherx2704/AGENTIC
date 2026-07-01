# environments/dev/postgres-bootstrap/terragrunt.hcl — Component 16 (Database Initialisation).
# Runs ONCE per env. Creates cypherx_platform DB, vector + pg_stat_statements extensions, the 7 schemas,
# and per-service *_user (runtime, least-priv) + *_ddl (CREATE,USAGE + CREATEROLE) users.
#
# Runtime/DDL passwords are injected as TF_VAR_<svc>_runtime_password / TF_VAR_<svc>_ddl_password from Doppler
# (db/<svc>/runtime_password, db/<svc>/ddl_password). Do NOT put passwords in this file.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/postgres-bootstrap.hcl"
  expose = true
}

inputs = {}
