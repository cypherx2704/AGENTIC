# ---------------------------------------------------------------------------------------------------------------------
# ROOT TERRAGRUNT CONFIGURATION
# ---------------------------------------------------------------------------------------------------------------------
# This is the single root config that every stack in environments/<env>/<stack>/terragrunt.hcl inherits from via
# `include "root" { path = find_in_parent_folders() }`.
#
# Responsibilities (Component 2 — Terraform Remote State):
#   - Configure the S3 + DynamoDB remote backend (cypherx-terraform-state-<account-id> / cypherx-terraform-locks).
#     These resources are themselves provisioned by modules/tfstate-backend (Group G1) — this file only consumes them.
#   - Generate the `provider "aws"` block + `required_providers` so every leaf stack pins identical versions.
#   - Expose common locals (env name, region, account id, tags) read from env.hcl up the tree.
#
# Terraform >= 1.9, AWS provider ~> 5.x are pinned here and in each module's versions.tf.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  # Read the per-environment configuration (environments/<env>/env.hcl). Every env dir MUST contain env.hcl.
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))

  env        = local.env_vars.locals.env
  aws_region = local.env_vars.locals.aws_region
  account_id = local.env_vars.locals.account_id

  # Project-wide constants (do not vary by env).
  project = "cypherx"

  # State bucket + lock table are created by modules/tfstate-backend (Component 2, Group G1).
  state_bucket     = "cypherx-terraform-state-${local.account_id}"
  state_lock_table = "cypherx-terraform-locks"

  # Tags applied to every resource that supports them, merged with module-specific tags downstream.
  common_tags = {
    Project     = local.project
    Environment = local.env
    ManagedBy   = "terragrunt"
    Repo        = "cypherx-infra"
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# REMOTE STATE — S3 backend + DynamoDB lock table (Component 2).
# `key` is path-keyed so each stack gets its own state file:
#   cypherx/<env>/<stack-relative-path>/terraform.tfstate
# ---------------------------------------------------------------------------------------------------------------------
remote_state {
  backend = "s3"

  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }

  config = {
    bucket         = local.state_bucket
    key            = "cypherx/${local.env}/${path_relative_to_include()}/terraform.tfstate"
    region         = local.aws_region
    encrypt        = true
    dynamodb_table = local.state_lock_table

    # SSE-KMS with the aws/s3 managed key (Component 2). Bucket policy + versioning live in tfstate-backend.
    s3_bucket_tags      = local.common_tags
    dynamodb_table_tags = local.common_tags
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# PROVIDER GENERATION — one generated provider block, identical across all stacks.
# AWS creds are sourced from the assumed TerraformInfraRole / TerraformIAMRole (Component 1) at apply time —
# NEVER hardcoded here. The `assume_role` is left to the caller's AWS_PROFILE / OIDC web-identity so this file
# stays creds-free.
# ---------------------------------------------------------------------------------------------------------------------
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    provider "aws" {
      region = "${local.aws_region}"

      # Guard against applying into the wrong account.
      allowed_account_ids = ["${local.account_id}"]

      default_tags {
        tags = {
          Project     = "${local.common_tags.Project}"
          Environment = "${local.common_tags.Environment}"
          ManagedBy   = "${local.common_tags.ManagedBy}"
          Repo        = "${local.common_tags.Repo}"
        }
      }
    }
  EOF
}

# ---------------------------------------------------------------------------------------------------------------------
# VERSION PINS — generated into every stack so `required_providers` is consistent platform-wide.
# Per-stack providers that are NOT aws (postgresql, kafka, doppler) are added by those stacks' own versions.tf;
# this block only pins the always-present aws + the Terraform core constraint.
# ---------------------------------------------------------------------------------------------------------------------
generate "versions" {
  path      = "versions_generated.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    terraform {
      required_version = ">= 1.9"

      required_providers {
        aws = {
          source  = "hashicorp/aws"
          version = "~> 5.60"
        }
      }
    }
  EOF
}

# ---------------------------------------------------------------------------------------------------------------------
# COMMON INPUTS — merged into every stack's inputs. Stacks override / extend in their own terragrunt.hcl.
# ---------------------------------------------------------------------------------------------------------------------
inputs = {
  env         = local.env
  aws_region  = local.aws_region
  account_id  = local.account_id
  common_tags = local.common_tags
}
