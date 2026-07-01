// Contract 14 — Atlas project config (reference template).
//
// Copy into <service-repo>/db/atlas.hcl and replace `example` with your service name.
// Atlas docs: https://atlasgo.io
//
// This config wires:
//   * the declarative desired-state snapshot (schema.sql),
//   * the versioned migration directory (migrations/),
//   * a throwaway "dev" database Atlas uses to compute diffs,
//   * the destructive-change lint policy enforced in CI (Contract 14 §3).

variable "url" {
  type        = string
  default     = getenv("DATABASE_URL")
  description = "Target database URL. In CI/runtime this is built from the DDL credentials in Doppler (db/<service>/ddl_password)."
}

variable "dev_url" {
  type        = string
  default     = "docker://postgres/16/dev"
  description = "Throwaway dev database Atlas uses to normalize and diff schemas. Never a real environment."
}

// Declarative desired state: the snapshot Atlas diffs against the migration history.
data "external_schema" "example" {
  program = [
    "cat",
    "schema.sql",
  ]
}

env "example" {
  // Desired state.
  src = data.external_schema.example.url

  // Target + dev databases.
  url     = var.url
  dev     = var.dev_url

  // Versioned migration directory (Contract 14 §2 convention).
  migration {
    dir = "file://migrations"
  }

  // Drift / destructive-change linting (Contract 14 §3 CI gate).
  lint {
    destructive {
      // Destructive changes fail lint unless annotated with `# atlas:nolint destructive`.
      error = true
    }
  }

  // Deterministic diff output for the schema-diff CI gate.
  diff {
    skip {
      drop_schema = true
    }
  }
}
