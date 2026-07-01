# ---------------------------------------------------------------------------------------------------------------------
# modules/doppler-bootstrap/versions.tf — Components 11 + 20 (Secrets Bootstrap in Doppler).
# Uses the official DopplerHQ/doppler provider. Pinned.
# ---------------------------------------------------------------------------------------------------------------------
terraform {
  required_version = ">= 1.9"

  required_providers {
    doppler = {
      source  = "DopplerHQ/doppler"
      version = "~> 1.12"
    }
  }
}
