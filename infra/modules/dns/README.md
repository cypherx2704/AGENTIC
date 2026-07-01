# Module: `dns`

Component 5 — **DNS & TLS domains**.

## What it provisions

| Resource | Detail |
|----------|--------|
| `aws_route53_zone.root` | Public hosted zone `cypherx.ai` (delegated from registrar). Account-global — created by exactly one stack (prod by convention); other envs pass `hosted_zone_id`. |
| `aws_acm_certificate.env_wildcard` | Per-env wildcard `*.<env>.cypherx.ai` (+ `<env>.cypherx.ai` SAN), DNS-validated, auto-renewed, in `us-east-1`. Attach to that env's public + internal ALBs. |
| `aws_acm_certificate.apex_wildcard` | **prod only**: `cypherx.ai` + `*.cypherx.ai` — covers the bare aliases. |
| `api.<env>` / `auth.<env>` records | ALIAS A → this env's public ALB (Kong). |
| `argocd.<env>` / `grafana.<env>` records | ALIAS A → this env's internal ALB (VPN-only). |
| `api.cypherx.ai` / `auth.cypherx.ai` | **prod only** bare aliases → prod public ALB. NOT present in dev/staging. |

## Hostname convention (locked in)

All environment hostnames are **env-scoped**: `api.<env>.cypherx.ai`,
`auth.<env>.cypherx.ai`. Prod additionally answers at the bare
`api.cypherx.ai` / `auth.cypherx.ai` so SDK/client defaults stay stable. **dev
and staging are never reachable at the env-less host** — this prevents a dev
token from being accepted by a misrouted prod client.

## The `iss` (stable) vs JWKS-URL (per-env) split — Component 5 / Contract 1

This is the most important contract this module encodes, recorded here so Phase 2
verifier configuration honours it:

- **`iss` is a stable identity, not a URL to fetch from.** Every JWT carries
  `iss: https://auth.cypherx.ai` regardless of environment. Verifiers MUST treat
  `iss` as an **opaque string** and compare it against their configured
  `AUTH_ISSUER_URL` — they MUST NOT derive the JWKS location from it.
- **JWKS is discovered per-env.** Each environment fetches keys from its **own**
  host: `https://auth.<env>.cypherx.ai/.well-known/jwks.json`. This is configured
  per-env (e.g. via `AUTH_JWKS_URL`), independent of the `iss` value.
- Outputs make the split explicit:
  - `jwt_issuer_url` → `https://auth.cypherx.ai` (stable, all envs)
  - `jwks_url`       → `https://auth.<env>.cypherx.ai/.well-known/jwks.json` (per-env)

Even in prod, where the bare `auth.cypherx.ai` alias exists, the alias is a
**routing convenience**; the issuer→JWKS mapping is still resolved from local
per-env config, never inferred from the `iss` string. Do not "simplify" Phase 2
verifier config to fetch JWKS from `iss` — that breaks dev/staging isolation.

## external-dns interplay

`manage_app_records = true` makes Terraform own the `api/auth/argocd/grafana`
ALIAS records. In environments where `external-dns` (Component 17b) creates these
from K8s Ingress/Service annotations at runtime, set `manage_app_records = false`
— the hosted zone and ACM cert are still managed here, only the app records are
delegated to external-dns. The two must not both manage the same record name.

## Inputs (highlights)

| Name | Default | Notes |
|------|---------|-------|
| `env` | — | dev/staging/prod |
| `root_domain` | `cypherx.ai` | |
| `create_hosted_zone` | `false` | one stack owns the zone |
| `hosted_zone_id` | `null` | required when not creating the zone |
| `public_alb_dns_name` / `public_alb_zone_id` | `null` | env public ALB (Kong) |
| `internal_alb_dns_name` / `internal_alb_zone_id` | `null` | env internal ALB |
| `manage_app_records` | `true` | false ⇒ external-dns owns app records |

## Outputs

`hosted_zone_id`, `hosted_zone_name_servers`, `env_wildcard_certificate_arn`,
`apex_wildcard_certificate_arn`, `api_host`, `auth_host`, `argocd_host`,
`grafana_host`, `jwt_issuer_url`, `jwks_url`.
