# Component 5 — DNS & TLS.
#
# Route53 hosted zone cypherx.ai (delegated from registrar) + per-env ACM
# wildcard *.<env>.cypherx.ai + env-scoped records (api/auth/argocd/grafana) +
# prod-only bare aliases (api.cypherx.ai, auth.cypherx.ai).
#
# ---------------------------------------------------------------------------
# iss (stable identity) vs JWKS URL (per-env resolution) — Component 5 / Contract 1
# ---------------------------------------------------------------------------
# The JWT `iss` claim is a STABLE issuer IDENTIFIER (`https://auth.cypherx.ai`)
# and MUST be treated by verifiers as an opaque string. It does NOT determine
# where JWKS is fetched. Each environment discovers JWKS at its OWN env-scoped
# host: `https://auth.<env>.cypherx.ai/.well-known/jwks.json`, configured
# per-env (not derived from `iss`). That is why dev/staging are NEVER reachable
# at the bare `auth.cypherx.ai` host — only prod gets the bare alias, and even
# then the alias is a routing convenience, not the issuer-to-JWKS mapping.
# Phase 2 verifier config encodes this split explicitly. See README.

locals {
  zone_id     = var.create_hosted_zone ? aws_route53_zone.root[0].zone_id : var.hosted_zone_id
  env_domain  = "${var.env}.${var.root_domain}"
  is_prod     = var.env == "prod"
  manage_apps = var.manage_app_records && var.public_alb_dns_name != null

  # Env-scoped hostnames (locked-in hostname convention).
  host_api     = "api.${local.env_domain}"
  host_auth    = "auth.${local.env_domain}"
  host_argocd  = "argocd.${local.env_domain}"
  host_grafana = "grafana.${local.env_domain}"

  common_tags = merge(var.tags, {
    Environment = var.env
    ManagedBy   = "terraform"
    Module      = "dns"
  })
}

# ---------------------------------------------------------------------------
# Hosted zone (account-global; one stack owns it, others pass the ID)
# ---------------------------------------------------------------------------
resource "aws_route53_zone" "root" {
  count = var.create_hosted_zone ? 1 : 0
  name  = var.root_domain

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Per-env wildcard ACM certificate: *.<env>.cypherx.ai
# Also covers the apex env host (<env>.cypherx.ai) as a SAN.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate" "env_wildcard" {
  domain_name               = "*.${local.env_domain}"
  subject_alternative_names = [local.env_domain]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(local.common_tags, { Name = "*.${local.env_domain}" })
}

resource "aws_route53_record" "env_wildcard_validation" {
  for_each = {
    for dvo in aws_acm_certificate.env_wildcard.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = local.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "env_wildcard" {
  certificate_arn         = aws_acm_certificate.env_wildcard.arn
  validation_record_fqdns = [for r in aws_route53_record.env_wildcard_validation : r.fqdn]
}

# ---------------------------------------------------------------------------
# Prod-only apex wildcard cert: cypherx.ai + *.cypherx.ai
# Covers the bare aliases api.cypherx.ai / auth.cypherx.ai.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate" "apex_wildcard" {
  count = local.is_prod ? 1 : 0

  domain_name               = var.root_domain
  subject_alternative_names = ["*.${var.root_domain}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(local.common_tags, { Name = var.root_domain })
}

resource "aws_route53_record" "apex_wildcard_validation" {
  for_each = local.is_prod ? {
    for dvo in aws_acm_certificate.apex_wildcard[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id         = local.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "apex_wildcard" {
  count = local.is_prod ? 1 : 0

  certificate_arn         = aws_acm_certificate.apex_wildcard[0].arn
  validation_record_fqdns = [for r in aws_route53_record.apex_wildcard_validation : r.fqdn]
}

# ---------------------------------------------------------------------------
# Env-scoped public records -> this env's public ALB (Kong)
# ---------------------------------------------------------------------------
resource "aws_route53_record" "api" {
  count   = local.manage_apps ? 1 : 0
  zone_id = local.zone_id
  name    = local.host_api
  type    = "A"

  alias {
    name                   = var.public_alb_dns_name
    zone_id                = var.public_alb_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "auth" {
  count   = local.manage_apps ? 1 : 0
  zone_id = local.zone_id
  name    = local.host_auth
  type    = "A"

  # JWKS at https://auth.<env>.cypherx.ai/.well-known/jwks.json (Contract 1).
  alias {
    name                   = var.public_alb_dns_name
    zone_id                = var.public_alb_zone_id
    evaluate_target_health = true
  }
}

# ---------------------------------------------------------------------------
# Env-scoped internal records -> this env's internal ALB (VPN-only)
# ---------------------------------------------------------------------------
resource "aws_route53_record" "argocd" {
  count   = local.manage_apps && var.internal_alb_dns_name != null ? 1 : 0
  zone_id = local.zone_id
  name    = local.host_argocd
  type    = "A"

  alias {
    name                   = var.internal_alb_dns_name
    zone_id                = var.internal_alb_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "grafana" {
  count   = local.manage_apps && var.internal_alb_dns_name != null ? 1 : 0
  zone_id = local.zone_id
  name    = local.host_grafana
  type    = "A"

  alias {
    name                   = var.internal_alb_dns_name
    zone_id                = var.internal_alb_zone_id
    evaluate_target_health = true
  }
}

# ---------------------------------------------------------------------------
# Prod-only bare aliases (NOT present in dev/staging).
#   api.cypherx.ai  -> api.prod.cypherx.ai
#   auth.cypherx.ai -> auth.prod.cypherx.ai
# These keep SDK/client defaults stable. dev/staging are intentionally NEVER
# reachable at the env-less host — prevents dev tokens reaching a misrouted
# prod client.
# ---------------------------------------------------------------------------
resource "aws_route53_record" "bare_api" {
  count   = local.is_prod && local.manage_apps ? 1 : 0
  zone_id = local.zone_id
  name    = "api.${var.root_domain}"
  type    = "A"

  alias {
    name                   = var.public_alb_dns_name
    zone_id                = var.public_alb_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "bare_auth" {
  count   = local.is_prod && local.manage_apps ? 1 : 0
  zone_id = local.zone_id
  name    = "auth.${var.root_domain}"
  type    = "A"

  alias {
    name                   = var.public_alb_dns_name
    zone_id                = var.public_alb_zone_id
    evaluate_target_health = true
  }
}
