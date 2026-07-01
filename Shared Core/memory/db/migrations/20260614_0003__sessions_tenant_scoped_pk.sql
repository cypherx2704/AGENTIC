-- =====================================================================================
-- memory-service — make memory.sessions keyed PER-TENANT (tenant_id, session_id).  PostgreSQL 16.
--
-- WHY THIS EXISTS
-- ---------------
-- memory.sessions originally had a GLOBAL primary key `session_id VARCHAR(128) PRIMARY KEY`
-- while the table is also RLS-protected on tenant_id. create_session() does a tenant-scoped
-- SELECT (which RLS confines to the caller's tenant) and then INSERTs on a miss. If ANOTHER
-- tenant already owns that session_id, RLS hides their row from the SELECT, the INSERT hits the
-- GLOBAL PK, and an unhandled UniqueViolation surfaced as a 500 — AND the 500-vs-201 difference
-- let one tenant probe whether a session_id exists in another tenant (a cross-tenant existence
-- side-channel). pg_repository.create_session now catches the UniqueViolation and maps it to the
-- existing 409 (kills the 500); THIS migration is the durable fix that also closes the side-channel
-- by making session_id unique only WITHIN a tenant, so cross-tenant ids never collide.
--
-- ADDITIVE + IDEMPOTENT: only swaps the PK column set from (session_id) to (tenant_id, session_id).
-- No data is moved or dropped; nothing references memory.sessions(session_id) as a foreign key
-- (memories.session_id is a plain column, not an FK). Safe to re-run: the DO block no-ops when the
-- PK is already tenant-scoped (or already migrated).
-- =====================================================================================

DO $$
DECLARE
  pk_cols text;
BEGIN
  SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
    INTO pk_cols
  FROM pg_constraint c
  JOIN pg_class t       ON t.oid = c.conrelid
  JOIN pg_namespace n   ON n.oid = t.relnamespace
  JOIN pg_attribute a   ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey)
  WHERE n.nspname = 'memory' AND t.relname = 'sessions' AND c.contype = 'p';

  IF pk_cols = 'session_id' THEN
    ALTER TABLE memory.sessions DROP CONSTRAINT sessions_pkey;
    ALTER TABLE memory.sessions ADD  CONSTRAINT sessions_pkey PRIMARY KEY (tenant_id, session_id);
    RAISE NOTICE 'memory.sessions PK migrated to (tenant_id, session_id)';
  ELSE
    RAISE NOTICE 'memory.sessions PK already %, no change', COALESCE(pk_cols, '(none)');
  END IF;
END $$;

-- =====================================================================================
-- end 20260614_0003__sessions_tenant_scoped_pk.sql
-- =====================================================================================
