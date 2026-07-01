# ---------------------------------------------------------------------------------------------------------------------
# modules/kafka-topics/versions.tf — Component 17 (Kafka Topic Bootstrap).
# Uses the Mongey/kafka provider (declarative, idempotent, drift-detected — Component 17 mandates it over
# kafka-topics.sh). Pinned.
# ---------------------------------------------------------------------------------------------------------------------
terraform {
  required_version = ">= 1.9"

  required_providers {
    kafka = {
      source  = "Mongey/kafka"
      version = "~> 0.7"
    }
  }
}
