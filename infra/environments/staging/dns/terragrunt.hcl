# environments/staging/dns/terragrunt.hcl — Component 5 (Route53 + ACM). Staging: env-scoped only, NO bare aliases.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/dns.hcl"
  expose = true
}

inputs = {}
