// Atlas project config for rag-service migrations (Phase 5 / WP09).
// https://atlasgo.io/ — declarative + versioned migration tooling.
//
// The directory `db/migrations` holds versioned, top-to-bottom-runnable PostgreSQL 16 SQL:
//   20260611_0001__init.sql  — schema, tables (incl. pgvector chunk_vectors_1536 + HNSW), RLS, grants
//   20260611_0002__seed.sql  — rag.pricing unit-cost knobs + auth.service_acl edges
//
// `schema.sql` is a flattened snapshot of the desired end-state (init + seed concatenated),
// used as the declarative source-of-truth for `atlas schema apply` / drift detection.
//
// The dev DB image is pgvector/pgvector:pg16 — the `vector` extension is available.
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
  // A throwaway DB Atlas uses to compute diffs (a local pgvector/pgvector:pg16 works).
  default = "docker://postgres/16/dev?search_path=rag"
}

env "local" {
  url = var.url != "" ? var.url : "postgres://rag_user:localdev@localhost:5432/cypherx_platform?search_path=rag&sslmode=disable"
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
