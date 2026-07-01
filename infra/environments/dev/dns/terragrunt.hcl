# environments/dev/dns/terragrunt.hcl — Component 5 (Route53 + ACM). Dev: env-scoped only, NO bare aliases.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/dns.hcl"
  expose = true
}

# create_bare_aliases / prod_wildcard_san are driven false in _envcommon/dns.hcl for non-prod.
inputs = {}
