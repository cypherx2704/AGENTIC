# environments/dev/kafka/terragrunt.hcl — Component 5 (MSK). 3 brokers, dev instance kafka.t3.small.
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/kafka.hcl"
  expose = true
}

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}

inputs = {
  broker_node_instance_type = local.env_vars.locals.kafka_instance_type # kafka.t3.small in dev
}
