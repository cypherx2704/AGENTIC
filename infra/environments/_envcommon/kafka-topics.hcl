# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/kafka-topics.hcl — shared inputs for the Kafka topic bootstrap stack (Component 17).
# Uses the Mongey/kafka provider to declaratively create the Component 17 core topics + DLQs. Idempotent,
# drift-detected. The topic SET is identical across envs; only the bootstrap brokers + SASL creds differ.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//kafka-topics"
}

dependency "kafka" {
  config_path = "../kafka"

  mock_outputs = {
    bootstrap_brokers_sasl_scram = "b-1.mock:9096,b-2.mock:9096,b-3.mock:9096"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  env = local.env

  # MSK SASL_SSL bootstrap brokers (port 9096 for SCRAM-SHA-512).
  bootstrap_servers = split(",", dependency.kafka.outputs.bootstrap_brokers_sasl_scram)

  # SASL SCRAM-SHA-512 admin creds from Doppler (the MSK admin user). Injected via TF_VAR_kafka_sasl_username /
  # TF_VAR_kafka_sasl_password — NEVER hardcoded.
  tls_enabled    = true
  sasl_mechanism = "scram-sha512"

  # Replication factor for MSK 3-broker cluster (Component 17: all core topics replication = 3).
  default_replication_factor = 3
}
