# ---------------------------------------------------------------------------------------------------------------------
# modules/doppler-bootstrap/main.tf — Components 11 (Doppler Operator) + 20 (Secrets Bootstrap in Doppler).
#
# Creates: the cypherx-platform project, the per-env config (dev|staging|prod), per-(env,namespace) operator
# service tokens, and the MANDATORY secret PATHS (Component 20) as placeholders.
#
# >>> ONE-TIME HUMAN BOOTSTRAP (Component 11) — see README. The provider's DOPPLER_TOKEN is a personal operator
#     token on the FIRST apply, then this stack writes the long-lived Terraform service token to ci/doppler_api_token
#     and CI reads it from there on every apply afterward. The personal token MUST be revoked after the first apply.
#
# Secret VALUES are placeholders. `ignore_changes = [value]` means re-applies create missing paths but NEVER
# overwrite a real value an operator/rotation has set. Terraform owns path EXISTENCE, not the live secret material.
# ---------------------------------------------------------------------------------------------------------------------

# Provider auth (DOPPLER_TOKEN) is taken from the environment — personal token (first apply) or ci/doppler_api_token
# (subsequent applies). NEVER hardcoded here.
provider "doppler" {}

locals {
  # ---------------------------------------------------------------------------------------------------------------
  # Component 20 mandatory secret PATHS. Doppler stores secrets as flat NAME=value within a config; the "path"
  # convention is encoded as the secret NAME using the slash-style path the Helm charts resolve by.
  # We model each path as a doppler_secret.name. Values are placeholders (see ignore_changes).
  # ---------------------------------------------------------------------------------------------------------------

  # service-auth/<svc>/bootstrap_secret  (Contract 12 — service-to-service auth) — for every service.
  bootstrap_secret_paths = {
    for svc in var.services : "service-auth/${svc}/bootstrap_secret" => svc
  }

  # db/<svc>/runtime_password + db/<svc>/ddl_password (Contract 14, Component 14) — for every DB-owning service.
  db_runtime_paths = {
    for svc in var.db_services : "db/${svc}/runtime_password" => svc
  }
  db_ddl_paths = {
    for svc in var.db_services : "db/${svc}/ddl_password" => svc
  }

  # CI paths (Component 18 + Component 11). Single instance each.
  ci_paths = {
    "ci/github_app_private_key" = "github-app"
    "ci/doppler_api_token"      = "doppler-operator-bootstrap"
  }

  # All placeholder secret paths merged for a single for_each.
  all_secret_paths = merge(
    local.bootstrap_secret_paths,
    local.db_runtime_paths,
    local.db_ddl_paths,
    local.ci_paths,
  )
}

# ---------------------------------------------------------------------------------------------------------------------
# PROJECT — cypherx-platform (Component 20). Created once; the dev/staging/prod stacks all reference the same
# project, so this resource is import-safe / no-op after the first env creates it.
# ---------------------------------------------------------------------------------------------------------------------
resource "doppler_project" "platform" {
  name        = var.project_name
  description = var.project_description
}

# ---------------------------------------------------------------------------------------------------------------------
# CONFIG — one per env (dev|staging|prod). Doppler ships root configs named after the environment; we use the env
# name directly as the config (with promotion between them handled in the Doppler UI/CLI — Component 20).
# ---------------------------------------------------------------------------------------------------------------------
resource "doppler_environment" "env" {
  project = doppler_project.platform.name
  slug    = var.config_name
  name    = title(var.config_name)
}

resource "doppler_config" "env" {
  project     = doppler_project.platform.name
  environment = doppler_environment.env.slug
  name        = var.config_name
}

# ---------------------------------------------------------------------------------------------------------------------
# SECRET PATHS — Component 20 placeholders. Real values set by the operator after bootstrap; Terraform never
# overwrites them (ignore_changes = [value]). The secret NAME encodes the slash-path the Helm charts resolve by.
# ---------------------------------------------------------------------------------------------------------------------
resource "doppler_secret" "path" {
  for_each = local.all_secret_paths

  project = doppler_project.platform.name
  config  = doppler_config.env.name
  name    = each.key
  value   = var.placeholder_value

  lifecycle {
    # Never clobber a real value an operator/rotation has written. Terraform owns existence, not the live material.
    ignore_changes = [value]
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# SERVICE TOKENS — one per (env, namespace) for the Doppler K8s operator (Component 11). Read-only; scoped to this
# env's config. The token value is sensitive output, consumed by the operator-bootstrap stack (k8s-addons) to seed
# the operator's K8s Secret — NOT via manual kubectl.
# ---------------------------------------------------------------------------------------------------------------------
resource "doppler_service_token" "operator" {
  for_each = toset(var.service_token_namespaces)

  project = doppler_project.platform.name
  config  = doppler_config.env.name
  name    = "operator-${var.env}-${each.value}"
  access  = "read"
}
