# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/doppler-bootstrap.hcl — shared inputs for the Doppler secrets bootstrap stack (Components 11, 20).
# Uses the Doppler terraform provider to create the cypherx-platform project, the dev/staging/prod configs,
# per-(env,namespace) service tokens, and the mandatory secret PATHS as placeholders.
#
# ONE-TIME human bootstrap (Component 11): the FIRST apply per env is run by a platform operator with a personal
# DOPPLER_TOKEN. That apply writes the long-lived Terraform service token to ci/doppler_api_token. From the second
# apply, CI reads ci/doppler_api_token. The personal token MUST be revoked after. See module README.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//doppler-bootstrap"
}

inputs = {
  env = local.env

  project_name = "cypherx-platform"

  # The Doppler config name in this project for this env. dev/staging/prod.
  config_name = local.env

  # Namespaces that need a per-(env,namespace) operator service token (Component 11). Maps to the K8s namespaces in
  # Component 6 that run pods needing synced secrets.
  service_token_namespaces = [
    "shared-core",
    "xagent",
    "tools",
    "platform-mgmt",
    "px0-bridge",
  ]

  # The DOPPLER_TOKEN used by the provider is sourced from the env (personal token on first apply,
  # ci/doppler_api_token thereafter) — NEVER hardcoded in HCL.
}
