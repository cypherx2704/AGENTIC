#!/usr/bin/env sh
# Sub-agent orchestration flow check.
#
# Prints the state of every hop in PROMPT -> ORCHESTRATOR -> SUB-AGENTS, in order, so a broken
# link is obvious. Read it top-to-bottom: the first stage that looks wrong is the one to fix.
#
#   cd infra/compose
#   docker compose --profile migrate run --rm --no-deps --entrypoint sh -T migrate -c "$(cat check-subagents.sh)"
#
# Read-only: every statement is a SELECT.
set -e

psql "$MIGRATE_DATABASE_URL" -X -q -P pager=off <<'SQL'
\echo
\echo ##### 1. ORCHESTRATOR (auth = identity source of truth) #####
\echo -- Expect one per tenant: agent_type=orchestrator, active, can_manage=t.
select tenant_id::text as tenant, agent_id::text as orchestrator, status,
       ('orchestrator:manage' = any(allowed_scopes)) as can_manage
  from auth.agents where agent_type = 'orchestrator' order by tenant_id;

\echo
\echo ##### 2. SUB-AGENT IDENTITIES (auth) #####
\echo -- Created by the orchestrator; scopes are a subset of the parent orchestrator scopes.
select name, status, parent_orchestrator_id::text as parent, cardinality(allowed_scopes) as n_scopes
  from auth.agents where agent_type = 'sub_agent' order by name;

\echo
\echo ##### 3. THE ROSTER (xagent.agents) -- THIS is what the driver actually reads #####
\echo -- A sub-agent must land here as sub_agent + parent stamped + active, or it can never run.
select name, agent_type, status, llm_model,
       (parent_orchestrator_id is not null) as parent_stamped
  from xagent.agents where agent_type = 'sub_agent' order by name;

\echo
\echo ##### 3b. DESYNC DETECTOR -- rows here are DEAD-END sub-agents #####
\echo -- Active identity in auth, but not schedulable by the orchestrator. Empty = healthy.
select a.name,
       case when x.agent_id is null then 'NO RUNTIME ROW (invisible to roster)'
            when x.agent_type <> 'sub_agent' then 'runtime type = ' || x.agent_type
            when x.parent_orchestrator_id is null then 'runtime parent NOT stamped'
            when x.status <> 'active' then 'runtime status = ' || x.status
       end as problem
  from auth.agents a left join xagent.agents x on x.agent_id = a.agent_id
 where a.agent_type = 'sub_agent' and a.status = 'active'
   and (x.agent_id is null or x.agent_type <> 'sub_agent'
        or x.parent_orchestrator_id is null or x.status <> 'active');

\echo
\echo ##### 4. WORKFLOW RUNS (newest first) #####
select status, error_code, cost_usd, tokens_used, left(goal, 45) as goal
  from xagent.workflows order by created_at desc limit 5;

\echo
\echo ##### 5. DECOMPOSITION of the newest run (which plan shape was chosen) #####
select decomposition as method,
       jsonb_array_length(subtask_dag->'nodes') as nodes,
       jsonb_array_length(coalesce(subtask_dag->'edges', '[]'::jsonb)) as edges
  from xagent.workflows order by created_at desc limit 1;

\echo
\echo ##### 6. THE EXECUTION TREE of the newest run (per node) #####
\echo -- Every node should reach completed; assigned_agent_id proves it bound to a sub-agent.
select t.node_id, t.preset, t.status, t.assigned_agent_id::text as sub_agent,
       t.tokens_used, t.cost_usd, length(coalesce(t.output->>'summary','')) as summary_chars
  from xagent.workflow_tasks t
 where t.workflow_id = (select workflow_id from xagent.workflows order by created_at desc limit 1)
 order by t.created_at;

\echo
\echo ##### 6b. Node errors, if any (workflow_tasks.output carries the failure) #####
select t.node_id, t.status, left(t.output::text, 200) as output
  from xagent.workflow_tasks t
 where t.workflow_id = (select workflow_id from xagent.workflows order by created_at desc limit 1)
   and t.status not in ('completed', 'pending')
 order by t.created_at;

\echo
\echo ##### 7. CHILD TASKS -- proves each sub-agent ran a REAL task under its OWN identity #####
\echo -- Each row = one sub-agent task, parented to the workflow. has_parent=t proves the tree link.
select tk.agent_id::text as ran_as, tk.status, (tk.parent_task_id is not null) as has_parent
  from xagent.tasks tk
 where tk.workflow_id = (select workflow_id from xagent.workflows order by created_at desc limit 1)
 order by tk.created_at;

\echo
\echo ##### 8. FINAL ANSWER of the newest run #####
select left(coalesce(output->>'final', output->>'answer', output::text), 400) as final_answer
  from xagent.workflows order by created_at desc limit 1;
SQL
