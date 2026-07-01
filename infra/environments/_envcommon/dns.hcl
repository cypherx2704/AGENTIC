# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/dns.hcl — shared inputs for the Route53 + ACM stack (Component 5).
# Hostname convention is LOCKED: all env hostnames are env-scoped (api.<env>.cypherx.ai, auth.<env>.cypherx.ai).
# Prod ALSO gets the bare aliases (api.cypherx.ai, auth.cypherx.ai). dev/staging NEVER get the env-less hostname.
# ACM wildcard *.<env>.cypherx.ai per env; prod additionally cypherx.ai + *.cypherx.ai.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env

  is_prod = local.env == "prod"
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//dns"
}

inputs = {
  # Delegated hosted zone (created once; same zone object referenced by all envs).
  hosted_zone_name = "cypherx.ai"

  env = local.env

  # Env-scoped records (always present). The ALB/internal-ALB targets are wired by external-dns at runtime;
  # the dns module pre-creates the wildcard cert + zone and any static ALIAS placeholders.
  env_hostnames = [
    "api.${local.env}.cypherx.ai",
    "auth.${local.env}.cypherx.ai",
    "argocd.${local.env}.cypherx.ai",  # internal ALB, VPN-only
    "grafana.${local.env}.cypherx.ai", # internal ALB, VPN-only
  ]

  # ACM: per-env wildcard (us-east-1, DNS-validated, auto-renew).
  acm_subject_alternative_names = ["*.${local.env}.cypherx.ai"]

  # Prod-only: bare aliases + wildcard covering them. dev/staging MUST NOT set these (guard against dev tokens
  # being accepted by a misrouted prod client — Component 5 lock).
  create_bare_aliases = local.is_prod
  bare_alias_records = local.is_prod ? {
    "api.cypherx.ai"  = "api.prod.cypherx.ai"
    "auth.cypherx.ai" = "auth.prod.cypherx.ai"
  } : {}
  prod_wildcard_san = local.is_prod ? ["cypherx.ai", "*.cypherx.ai"] : []
}
