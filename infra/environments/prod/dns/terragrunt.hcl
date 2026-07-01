# environments/prod/dns/terragrunt.hcl — Component 5 (Route53 + ACM).
# Prod-ONLY: bare aliases (api.cypherx.ai → api.prod.cypherx.ai, auth.cypherx.ai → auth.prod.cypherx.ai) and the
# cypherx.ai + *.cypherx.ai wildcard cert. Driven by create_bare_aliases=true in _envcommon/dns.hcl when env==prod.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/dns.hcl"
  expose = true
}

inputs = {}
