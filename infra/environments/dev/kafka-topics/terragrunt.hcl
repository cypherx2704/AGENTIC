# environments/dev/kafka-topics/terragrunt.hcl — Component 17 (Kafka Topic Bootstrap).
# Declarative core topics + DLQs via Mongey/kafka provider. SASL creds via TF_VAR_kafka_sasl_username /
# TF_VAR_kafka_sasl_password from Doppler. Compact auth.agent.* topics: producers MUST key by agent_id (README).
include "root" {
  path = find_in_parent_folders()
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders())}/_envcommon/kafka-topics.hcl"
  expose = true
}

inputs = {}
