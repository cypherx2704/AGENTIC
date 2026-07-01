# environments/prod/kafka-topics/terragrunt.hcl — Component 17. Declarative topics + DLQs.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/kafka-topics.hcl"
  expose = true
}

inputs = {}
