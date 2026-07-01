-- =====================================================================================
-- cypherx-a1 — Autonomous Engineering Memory: initial schema. PostgreSQL 16 + pgcrypto.
--
-- Run as a superuser / migration role (cxa1_ddl). Creates the `cypherx_a1` schema, the
-- runtime role `cxa1_user`, the knowledge-graph + ingestion tables, Row Level Security
-- (Contract 13), and the grants the runtime role needs. Idempotent (re-runnable).
--
-- OWNERSHIP SPLIT (see docs/03-data-model-and-schema.md):
--   * The GRAPH + raw landing + connectors + extraction ledger live HERE (app-owned).
--   * VECTORS live in the SharedCore RAG service (this app stores only a vector_ref).
--   * Copilot conversational memory lives in the SharedCore Memory service.
--
-- TENANT-SCOPED tables (tenant_id + tenant-leading index + RLS USING app.tenant_id):
--   entities, edges, identities, raw_events, connectors, connector_secrets,
--   sync_cursors, extraction_jobs, citations, resource_acls, rag_kbs
-- PLATFORM-INTERNAL table (NO RLS — internal cross-tenant publish queue):
--   outbox
--
-- The runtime role connects and runs every tenant-scoped query inside
--   BEGIN; SELECT set_config('app.tenant_id','<uuid>',true); ...; COMMIT
-- (the core in_tenant() helper). cxa1_user is NOT a superuser and does NOT BYPASSRLS.
-- Policies use current_setting('app.tenant_id', true) with NULLIF so an unset GUC yields
-- no rows (never errors). Runtime role cannot CREATE EXTENSION (frozen image) — extensions
-- are created here by the migration role only.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS cypherx_a1;

-- ── Runtime role (the app connects as this; created idempotently) ─────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cxa1_user') THEN
    CREATE ROLE cxa1_user LOGIN;
  END IF;
END
$$;

-- Default the runtime role's search_path to the app schema. This is the POOLED-SAFE way to
-- pin the schema: Neon's pgbouncer endpoint REJECTS the libpq `options=-c search_path=...`
-- startup parameter ("unsupported startup parameter in options"), so the DSN must NOT carry
-- it. A role-level default is applied on the server-side backend connection and works through
-- transaction pooling. (App queries are schema-qualified anyway; this covers any unqualified
-- reference and keeps the DATABASE_URL clean: ...sslmode=require with no options param.)
ALTER ROLE cxa1_user SET search_path = cypherx_a1, public;

GRANT USAGE ON SCHEMA cypherx_a1 TO cxa1_user;

-- =====================================================================================
-- TENANT-SCOPED TABLES
-- =====================================================================================

-- ── entities — the knowledge-graph nodes (bitemporal) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.entities (
  entity_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL,
  kind         VARCHAR(20)  NOT NULL,
  source       VARCHAR(40)  NOT NULL,                 -- github | jira | slack | ...
  external_id  TEXT,                                  -- id in the source system
  natural_key  TEXT         NOT NULL,                 -- stable dedup key within (tenant,kind)
  title        TEXT,
  search_text  TEXT,                                  -- normalized text for keyword search
  body_ref     JSONB,                                 -- {bucket,key} S3 pointer for large bodies
  attrs        JSONB        NOT NULL DEFAULT '{}',
  vector_ref   JSONB,                                 -- {kb_id,doc_id,chunk_id} in RAG (delegated)
  content_sha  TEXT,
  valid_from   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  valid_to     TIMESTAMPTZ,                           -- NULL = current version (bitemporal)
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  fts          tsvector GENERATED ALWAYS AS
                 (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(search_text,''))) STORED,

  CONSTRAINT entities_kind_enum CHECK (kind IN
    ('person','service','repo','feature','decision','incident','pr','ticket','document'))
);
-- Upsert/dedup key: one current row per (tenant, kind, natural_key) (enforced app-side on
-- the current slice; older bitemporal versions keep valid_to set).
CREATE UNIQUE INDEX IF NOT EXISTS uq_entities_natural_current
  ON cypherx_a1.entities (tenant_id, kind, natural_key) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_entities_tenant       ON cypherx_a1.entities (tenant_id);
CREATE INDEX IF NOT EXISTS idx_entities_tenant_kind  ON cypherx_a1.entities (tenant_id, kind);
CREATE INDEX IF NOT EXISTS idx_entities_fts          ON cypherx_a1.entities USING GIN (fts);
CREATE INDEX IF NOT EXISTS idx_entities_attrs        ON cypherx_a1.entities USING GIN (attrs);

-- ── edges — typed bitemporal relationships (adjacency list) ────────────────────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.edges (
  edge_id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID         NOT NULL,
  src_entity_id     UUID         NOT NULL,
  dst_entity_id     UUID         NOT NULL,
  rel               VARCHAR(20)  NOT NULL,
  confidence        NUMERIC(4,3) NOT NULL DEFAULT 1.000,
  extractor_version VARCHAR(20)  NOT NULL DEFAULT 'ingest',
  evidence_chunk_ids UUID[]      NOT NULL DEFAULT '{}',
  metadata          JSONB        NOT NULL DEFAULT '{}',
  valid_from        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  valid_to          TIMESTAMPTZ,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  CONSTRAINT edges_rel_enum CHECK (rel IN
    ('owns','authored','reviewed','depends_on','caused','resolved','mentions',
     'decided_in','deployed','expert_in','part_of'))
);
-- Bidirectional recursive-CTE traversal + a partial index for the CURRENT graph.
CREATE INDEX IF NOT EXISTS idx_edges_src ON cypherx_a1.edges (tenant_id, src_entity_id, rel);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON cypherx_a1.edges (tenant_id, dst_entity_id, rel);
CREATE INDEX IF NOT EXISTS idx_edges_current
  ON cypherx_a1.edges (tenant_id, src_entity_id, rel) WHERE valid_to IS NULL;
-- Hot path for impact_of() — the reverse-depends_on blast-radius recursive CTE traverses by
-- dst_entity_id filtered to rel='depends_on' on the current slice. A dedicated partial index
-- keeps each recursive iteration index-only even at high depends_on cardinality.
CREATE INDEX IF NOT EXISTS idx_edges_depends_on_current
  ON cypherx_a1.edges (tenant_id, dst_entity_id) WHERE rel = 'depends_on' AND valid_to IS NULL;

-- ── identities — cross-tool alias resolution (-> canonical person entity) ──────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.identities (
  identity_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID        NOT NULL,
  person_entity_id UUID     NOT NULL,
  source        VARCHAR(40) NOT NULL,                 -- github | slack | jira | email
  handle        TEXT        NOT NULL,                 -- login / uid / account id / email
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_identities UNIQUE (tenant_id, source, handle)
);
CREATE INDEX IF NOT EXISTS idx_identities_tenant ON cypherx_a1.identities (tenant_id);

-- ── raw_events — immutable landing / audit (idempotent on source+external_id+content_sha)
CREATE TABLE IF NOT EXISTS cypherx_a1.raw_events (
  raw_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL,
  source       VARCHAR(40)  NOT NULL,
  external_id  TEXT         NOT NULL,
  record_type  VARCHAR(40)  NOT NULL,                 -- commit | pull_request | issue | message
  content_sha  TEXT         NOT NULL,
  body_ref     JSONB,                                 -- S3 pointer for the raw payload
  payload      JSONB,                                 -- small inline payloads
  received_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_raw_events UNIQUE (tenant_id, source, external_id, content_sha)
);
CREATE INDEX IF NOT EXISTS idx_raw_events_tenant ON cypherx_a1.raw_events (tenant_id, received_at DESC);

-- ── connectors — per-tenant connector installs + config ────────────────────────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.connectors (
  connector_id UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID         NOT NULL,
  kind         VARCHAR(40)  NOT NULL,                 -- github | jira | slack | ...
  display_name VARCHAR(255) NOT NULL,
  config       JSONB        NOT NULL DEFAULT '{}',    -- non-secret config (org, repos, urls)
  status       VARCHAR(20)  NOT NULL DEFAULT 'active',
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT connectors_status_enum CHECK (status IN ('active','paused','error')),
  CONSTRAINT uq_connectors UNIQUE (tenant_id, kind, display_name)
);
CREATE INDEX IF NOT EXISTS idx_connectors_tenant ON cypherx_a1.connectors (tenant_id);

-- ── connector_secrets — KMS/BYOK-sealed credentials (sealed:v1 envelope) ───────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.connector_secrets (
  connector_id UUID         PRIMARY KEY,              -- 1:1 with connectors
  tenant_id    UUID         NOT NULL,
  sealed_value TEXT         NOT NULL,                 -- "sealed:v1:<...>" or "env:<NAME>"
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  rotated_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_connector_secrets_tenant ON cypherx_a1.connector_secrets (tenant_id);

-- ── sync_cursors — resumable per-(tenant,connector,stream) sync position ───────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.sync_cursors (
  tenant_id    UUID         NOT NULL,
  connector_id UUID         NOT NULL,
  stream       VARCHAR(60)  NOT NULL,                 -- e.g. repo:owner/name:pulls
  cursor       TEXT,                                  -- opaque connector cursor
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, connector_id, stream)
);

-- ── extraction_jobs — idempotency + cost ledger for LLM extraction ─────────────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.extraction_jobs (
  tenant_id         UUID        NOT NULL,
  node_id           UUID        NOT NULL,             -- entity the extraction ran over
  content_sha       TEXT        NOT NULL,
  extractor_version VARCHAR(20) NOT NULL,
  status            VARCHAR(20) NOT NULL DEFAULT 'completed',
  edges_extracted   INTEGER     NOT NULL DEFAULT 0,
  llm_call_id       TEXT,                             -- gateway billing key (Contract 19)
  cost_usd          NUMERIC(12,8) NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, node_id, content_sha, extractor_version),
  CONSTRAINT extraction_status_enum CHECK (status IN ('completed','failed','running'))
);

-- ── citations — RAG chunk / doc -> graph entity/edge provenance links ──────────────────
CREATE TABLE IF NOT EXISTS cypherx_a1.citations (
  citation_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID        NOT NULL,
  kb_id       TEXT        NOT NULL,
  doc_id      TEXT,
  chunk_id    TEXT,
  entity_id   UUID,
  edge_id     UUID,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_citations_tenant ON cypherx_a1.citations (tenant_id);
CREATE INDEX IF NOT EXISTS idx_citations_chunk  ON cypherx_a1.citations (tenant_id, chunk_id);

-- ── resource_acls — per-repo / per-team read rules (the tenancy decision) ──────────────
-- App-owned authorization on engineering entities. Auth never models repos/teams.
CREATE TABLE IF NOT EXISTS cypherx_a1.resource_acls (
  acl_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID        NOT NULL,
  resource_type VARCHAR(20) NOT NULL,                 -- repo | team | service
  resource_key  TEXT        NOT NULL,                 -- e.g. "owner/name"
  principal_type VARCHAR(20) NOT NULL,                -- agent | user | role | tenant
  principal_id  TEXT        NOT NULL,                 -- id or '*'
  permission    VARCHAR(20) NOT NULL DEFAULT 'read',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT resource_acls_principal_enum CHECK (principal_type IN ('agent','user','role','tenant')),
  CONSTRAINT uq_resource_acls UNIQUE (tenant_id, resource_type, resource_key, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_acls_lookup
  ON cypherx_a1.resource_acls (tenant_id, resource_type, resource_key);

-- ── rag_kbs — resolved RAG knowledge-base bindings (embedding model pinned, immutable) ─
CREATE TABLE IF NOT EXISTS cypherx_a1.rag_kbs (
  tenant_id              UUID        NOT NULL,
  logical_name           VARCHAR(60) NOT NULL,        -- eng-code | eng-conversations | ...
  kb_id                  TEXT        NOT NULL,
  embedding_model_resolved TEXT      NOT NULL,
  embedding_dim          INTEGER     NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, logical_name)
);

-- =====================================================================================
-- PLATFORM-INTERNAL TABLE (NO RLS — internal cross-tenant publish queue)
-- =====================================================================================
CREATE TABLE IF NOT EXISTS cypherx_a1.outbox (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  topic         VARCHAR(200) NOT NULL,
  partition_key VARCHAR(64)  NOT NULL,                -- tenant_id (Contract 5)
  payload       JSONB        NOT NULL,                -- Contract 5 envelope, ready to publish
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  published_at  TIMESTAMPTZ,
  attempts      INTEGER      NOT NULL DEFAULT 0,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cxa1_outbox_unpublished
  ON cypherx_a1.outbox (created_at) WHERE published_at IS NULL;

-- =====================================================================================
-- ROW LEVEL SECURITY (Contract 13) — enable + FORCE on every tenant-scoped table.
-- =====================================================================================
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'entities','edges','identities','raw_events','connectors','connector_secrets',
    'sync_cursors','extraction_jobs','citations','resource_acls','rag_kbs'
  ]
  LOOP
    EXECUTE format('ALTER TABLE cypherx_a1.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE cypherx_a1.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I_isolation ON cypherx_a1.%I', t, t);
    EXECUTE format(
      'CREATE POLICY %I_isolation ON cypherx_a1.%I FOR ALL '
      'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
      'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
      t, t
    );
  END LOOP;
END
$$;

-- outbox is an INTERNAL publish queue drained by a background task across ALL tenants;
-- tenant-RLS would block the drain (the publisher sets no app.tenant_id). Isolation is in
-- the payload, not the row. RLS intentionally NOT enabled on outbox.
ALTER TABLE cypherx_a1.outbox DISABLE ROW LEVEL SECURITY;

-- =====================================================================================
-- GRANTS to the runtime role (cxa1_user). RLS still applies on top of these.
-- =====================================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.entities          TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.edges             TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.identities        TO cxa1_user;
GRANT SELECT, INSERT                 ON cypherx_a1.raw_events         TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.connectors        TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.connector_secrets TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.sync_cursors      TO cxa1_user;
GRANT SELECT, INSERT, UPDATE         ON cypherx_a1.extraction_jobs    TO cxa1_user;
GRANT SELECT, INSERT, DELETE         ON cypherx_a1.citations          TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.resource_acls     TO cxa1_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON cypherx_a1.rag_kbs           TO cxa1_user;
GRANT SELECT, INSERT, UPDATE         ON cypherx_a1.outbox             TO cxa1_user;
