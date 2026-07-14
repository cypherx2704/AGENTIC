-- 0010 — DROP xagent.agent_presets (dead: never read, never written, never seeded).
--
-- WHY IT IS GOING
-- ---------------
-- agent_presets was introduced in 0008 as the ".claude/agents" analogue: reusable
-- {system_prompt, tools, scopes, model} bundles that a DAG node's `preset` would point at, so a
-- template could fan a goal out to a `researcher` / `writer` / `reviewer` trio.
--
-- That whole idea is gone. Routing is now the orchestrator LLM's decision alone: the planner is shown
-- the tenant's real sub-agents (name + description + actual tools) and names one per step. A node's
-- `preset` is therefore just THE AGENT'S NAME, resolved against the live roster in `xagent.agents` —
-- it never was, and now never will be, a foreign key into this table.
--
-- The table has been dead since it was created: nothing in the codebase reads it, writes it, or seeds
-- it (`create_preset` / `list_presets` in orchestration/repo.py had zero callers and are deleted in
-- this same change). Keeping a tenant-scoped, RLS'd table that no code path can populate is a
-- standing invitation to re-introduce preset-driven routing.
--
-- SAFETY
-- ------
-- This is destructive and irreversible, so it REFUSES to run if the table somehow holds rows. Nothing
-- in the application can have written any (there is no INSERT path and no seed), so a non-empty table
-- would mean a human put data there by hand — in which case a migration must stop and let a human
-- decide, not silently destroy it. An empty table drops cleanly, taking its index, RLS policy and
-- grants with it. Idempotent: a no-op if 0008 never ran.
DO $$
DECLARE
  row_count bigint;
BEGIN
  IF to_regclass('xagent.agent_presets') IS NULL THEN
    RAISE NOTICE '0010: xagent.agent_presets does not exist — nothing to drop.';
    RETURN;
  END IF;

  -- Count WITHOUT row-level filtering. The migration role owns the schema, so RLS does not apply to
  -- it anyway (the policy is ENABLE, not FORCE); this makes that explicit, and guarantees the guard
  -- below can never be fooled into seeing an empty table by a policy hiding rows. If some role
  -- without the exemption ever runs this, Postgres ERRORS here rather than under-counting — which is
  -- exactly the failure we want.
  SET LOCAL row_security = off;

  EXECUTE 'SELECT count(*) FROM xagent.agent_presets' INTO row_count;

  IF row_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'REFUSING TO DROP xagent.agent_presets: it holds %s row(s).', row_count),
      DETAIL  = 'No application code path can create these rows (there is no INSERT caller and no '
                'seed), so they were inserted by hand. Dropping the table would destroy them.',
      HINT    = 'Export/verify the rows, then either delete them and re-run this migration, or drop '
                'the table manually if the data is genuinely disposable.';
  END IF;

  EXECUTE 'DROP TABLE xagent.agent_presets';
  RAISE NOTICE '0010: dropped empty xagent.agent_presets (index, RLS policy and grants go with it).';
END $$;
