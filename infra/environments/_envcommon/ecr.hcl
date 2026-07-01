# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/ecr.hcl — shared inputs for the ECR repositories stack (Component 5).
# One repo per service. Initial 13 (Phase 1); later phases add skills/a2a/web-frontend per-phase, NOT here.
# Repo list is identical across envs; ECR is regional and shared by all clusters in the account.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env

  # Component 5 — exact initial 13 repositories.
  service_repositories = [
    "cypherx/auth-service",
    "cypherx/llms-gateway",
    "cypherx/guardrails-service",
    "cypherx/memory-service",
    "cypherx/rag-service",
    "cypherx/xagent",
    "cypherx/orchestrator",
    "cypherx/platform-management",
    "cypherx/tool-web-search",
    "cypherx/tool-code-exec",
    "cypherx/tool-http-client",
    "cypherx/tool-file-ops",
    "cypherx/px0-bridge",
  ]
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//ecr-repo"
}

inputs = {
  repository_names = local.service_repositories

  # Immutable tags — the Component 18 tagging convention relies on immutability (sha-/semver tags kept forever).
  image_tag_mutability = "IMMUTABLE"

  # Scan on push (Trivy in CI is the gate; native scan is defence-in-depth).
  scan_on_push = true

  # KMS encryption for image layers.
  encryption_type = "KMS"

  # Lifecycle: expire untagged + pr-* images, keep sha-/semver forever (Component 18 convention).
  lifecycle_policy_keep_last_pr_images = 10
}
