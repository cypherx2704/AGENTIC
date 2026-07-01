// Atlas project config for cypherx-a1 migrations (Contract 14).
// https://atlasgo.io/ — declarative + versioned migration tooling.
//
// db/migrations holds versioned, top-to-bottom-runnable PostgreSQL 16 SQL:
//   20260614_0001__init.sql  — schema, tables, indexes, RLS, grants
//   20260614_0002__seed.sql  — no-op (connectors/agents are created per-tenant at runtime)
//
// schema.sql is the flattened desired end-state snapshot (drift detection). The compose
// `migrate` job applies the versioned *.sql via psql (DIRECT Neon endpoint) like the other
// services; Atlas is for diff/drift in CI.
//
// Usage:
//   atlas migrate apply --env local
//   atlas schema apply  --env local --to file://schema.sql

variable "url" {
  type    = string
  default = getenv("DATABASE_URL")
}

variable "dev_url" {
  type    = string
  default = "docker://postgres/16/dev?search_path=cypherx_a1"
}

env "local" {
  url = var.url != "" ? var.url : "postgres://cxa1_user:localdev@localhost:5432/cypherx_platform?search_path=cypherx_a1&sslmode=disable"
  dev = var.dev_url
  src = "file://schema.sql"
  migration {
    dir = "file://."
  }
  format {
    migrate {
      diff = "{{ sql . \"  \" }}"
    }
  }
}

env "ci" {
  url = var.url
  dev = var.dev_url
  src = "file://schema.sql"
  migration {
    dir = "file://."
  }
}
