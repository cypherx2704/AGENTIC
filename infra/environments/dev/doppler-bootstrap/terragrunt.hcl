# environments/dev/doppler-bootstrap/terragrunt.hcl — Components 11 + 20 (Secrets Bootstrap in Doppler).
# Creates cypherx-platform project, dev config, per-(env,namespace) service tokens, and mandatory secret PATHS
# as placeholders. The FIRST dev apply is run by a platform operator with a personal DOPPLER_TOKEN (one-time
# human bootstrap — see module README); thereafter CI reads ci/doppler_api_token.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/doppler-bootstrap.hcl"
  expose = true
}

inputs = {}
