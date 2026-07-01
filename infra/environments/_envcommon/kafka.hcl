# ---------------------------------------------------------------------------------------------------------------------
# _envcommon/kafka.hcl — shared inputs for the MSK (Kafka) stack (Component 5).
# 3 brokers (one per AZ), gp3 100GB/broker, Kafka 3.6.x, TLS in-transit, KMS at-rest, SASL SCRAM-SHA-512.
# Broker instance type varies by env (dev: kafka.t3.small, prod: kafka.m5.large) via env.hcl.
# ---------------------------------------------------------------------------------------------------------------------

locals {
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  env      = local.env_vars.locals.env
}

terraform {
  source = "${dirname(find_in_parent_folders())}/../modules//kafka"
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    vpc_id             = "vpc-00000000000000000"
    private_subnet_ids = ["subnet-aaaa", "subnet-bbbb", "subnet-cccc"]
    eks_node_sg_id     = "sg-00000000000000000"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan", "init"]
}

inputs = {
  cluster_name = "cypherx-${local.env}"

  kafka_version = "3.6.0" # Component 5: 3.6.x.

  # Component 5: 3 brokers, one per AZ.
  number_of_broker_nodes     = 3
  broker_node_client_subnets = dependency.vpc.outputs.private_subnet_ids

  # Component 5: 100GB gp3 per broker.
  broker_node_storage_info = {
    ebs_storage_info = {
      volume_size = 100
    }
  }

  # Component 5: TLS in-transit.
  encryption_in_transit_client_broker = "TLS"
  encryption_in_transit_in_cluster    = true

  # Component 5: SASL SCRAM-SHA-512 auth.
  client_authentication = {
    sasl = {
      scram = true
    }
  }

  # Component 3 sg-kafka: inbound 9092,9094 from sg-eks-nodes only.
  allowed_security_group_ids = [dependency.vpc.outputs.eks_node_sg_id]
}
