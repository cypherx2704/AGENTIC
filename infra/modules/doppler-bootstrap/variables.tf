# ---------------------------------------------------------------------------------------------------------------------
# modules/doppler-bootstrap/variables.tf — Components 11 + 20.
# ---------------------------------------------------------------------------------------------------------------------

variable "env" {
  description = "Environment name (dev | staging | prod). Maps 1:1 to a Doppler config in the project."
  type        = string
}

variable "project_name" {
  description = "Doppler project name (Component 20)."
  type        = string
  default     = "cypherx-platform"
}

variable "project_description" {
  description = "Doppler project description."
  type        = string
  default     = "CypherX AI platform secrets — managed by Terraform (cypherx-infra/modules/doppler-bootstrap)."
}

variable "config_name" {
  description = "The Doppler config (environment) name for this env. Equals var.env (dev|staging|prod)."
  type        = string
}

variable "service_token_namespaces" {
  description = <<-EOT
    K8s namespaces that need a per-(env,namespace) Doppler operator service token (Component 11).
    One read-only service token is minted per namespace for the Doppler K8s operator to sync secrets.
  EOT
  type        = list(string)
}

variable "services" {
  description = <<-EOT
    Service names that require per-service secret paths (Component 20):
      service-auth/<svc>/bootstrap_secret, db/<svc>/runtime_password, db/<svc>/ddl_password.
    Defaults to the first-cycle SharedCore + xagent + orchestrator + platform-mgmt + tools + px0-bridge set.
  EOT
  type        = list(string)
  default = [
    "auth",
    "llms",
    "guardrails",
    "memory",
    "rag",
    "xagent",
    "orchestrator",
    "platform-mgmt",
    "px0-bridge",
    "tool-web-search",
    "tool-code-exec",
    "tool-http-client",
    "tool-file-ops",
  ]
}

variable "db_services" {
  description = <<-EOT
    Subset of services that own a Postgres schema and therefore need db/<svc>/runtime_password +
    db/<svc>/ddl_password paths (Component 14, 20). All SharedCore services with a schema.
  EOT
  type        = list(string)
  default = [
    "auth",
    "llms",
    "guardrails",
    "memory",
    "rag",
    "xagent",
    "platform-mgmt",
  ]
}

variable "placeholder_value" {
  description = <<-EOT
    Value written to every bootstrapped secret PATH on first create. A non-secret placeholder — the real value is
    set by the platform operator (rotation) AFTER bootstrap. Terraform manages the path's existence, not its value
    on every apply (see lifecycle ignore_changes in main.tf), so real secrets are never overwritten.
  EOT
  type        = string
  default     = "REPLACE_ME__set_by_operator_after_bootstrap"
}
