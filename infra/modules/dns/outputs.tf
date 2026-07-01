output "hosted_zone_id" {
  description = "Route53 hosted zone ID for the root domain (created or passed through)."
  value       = local.zone_id
}

output "hosted_zone_name_servers" {
  description = "Name servers for the hosted zone (set these as the registrar delegation). Empty when the zone is not created by this stack."
  value       = var.create_hosted_zone ? aws_route53_zone.root[0].name_servers : []
}

output "env_wildcard_certificate_arn" {
  description = "ARN of the validated per-env wildcard ACM cert (*.<env>.cypherx.ai). Attach to the env's public + internal ALBs."
  value       = aws_acm_certificate_validation.env_wildcard.certificate_arn
}

output "apex_wildcard_certificate_arn" {
  description = "ARN of the validated prod apex wildcard ACM cert (cypherx.ai + *.cypherx.ai). Null in dev/staging."
  value       = local.is_prod ? aws_acm_certificate_validation.apex_wildcard[0].certificate_arn : null
}

output "api_host" {
  description = "Env-scoped API hostname (api.<env>.cypherx.ai)."
  value       = local.host_api
}

output "auth_host" {
  description = "Env-scoped Auth hostname (auth.<env>.cypherx.ai). JWKS at /.well-known/jwks.json."
  value       = local.host_auth
}

output "argocd_host" {
  description = "Env-scoped ArgoCD hostname (argocd.<env>.cypherx.ai, internal/VPN-only)."
  value       = local.host_argocd
}

output "grafana_host" {
  description = "Env-scoped Grafana hostname (grafana.<env>.cypherx.ai, internal/VPN-only)."
  value       = local.host_grafana
}

output "jwt_issuer_url" {
  description = "STABLE JWT issuer identifier (https://auth.cypherx.ai), opaque to verifiers. NOT the per-env JWKS host — see README iss-vs-JWKS split."
  value       = "https://auth.${var.root_domain}"
}

output "jwks_url" {
  description = "Per-env JWKS discovery URL (https://auth.<env>.cypherx.ai/.well-known/jwks.json). Verifiers configure this per-env; it is NOT derived from iss."
  value       = "https://${local.host_auth}/.well-known/jwks.json"
}
