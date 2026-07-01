// Atlas project config for guardrails-service migrations (Phase 4).
// https://atlasgo.io/ — declarative + versioned migration tooling.
//
// The directory `db/migrations` holds versioned, top-to-bottom-runnable PostgreSQL 16 SQL:
//   20260608_0001__init.sql  — schema, tables, indexes, RLS, grants
//   20260608_0002__seed.sql  — 11 rule rows + the one platform-default policy
//
// `schema.sql` is a flattened snapshot of the desired end-state (init + seed concatenated),
// used as the declarative source-of-truth for `atlas schema apply` / drift detection.
//
// Usage:
//   atlas migrate apply --env local
//   atlas schema apply  --env local --to file://schema.sql
//   atlas migrate diff  --env local           // generate a new versioned file from schema.sql

variable "url" {
  type    = string
  default = getenv("DATABASE_URL")
}

variable "dev_url" {
  type    = string
  // A throwaway DB Atlas uses to compute diffs (a local docker postgres:16 works).
  default = "docker://postgres/16/dev?search_path=guardrails"
}

env "local" {
  url = var.url != "" ? var.url : "postgres://grd_user:localdev@localhost:5432/cypherx_platform?search_path=guardrails&sslmode=disable"
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
