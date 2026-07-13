/**
 * Runtime-registration helpers for sub-agents.
 *
 * THE INVARIANT THIS FILE EXISTS TO ENFORCE: creating a sub-agent takes TWO writes —
 *   1. the IDENTITY in auth  (POST /v1/orchestrator/sub-agents) — sets agent_type + parent, and
 *   2. the RUNTIME in xAgent (PUT  /v1/agents/{id}/runtime)     — the row the scheduler reads.
 *
 * The orchestration roster is built from `xagent.agents`, so an identity WITHOUT a runtime row can
 * never be scheduled — the run just fails UNASSIGNED_NODE. Every sub-agent creation path must go
 * through `subAgentRegistration()` to produce a complete, active runtime body.
 *
 * There are deliberately NO hardcoded tool names here: tools are tenant-scoped (e.g.
 * `tool-wikipedia-summary-<tenant>`), so a static default cannot know them. Attach real tools
 * per sub-agent in the Agent Builder.
 */
import type { AgentRuntimeRegistration, AgentRuntime, MemoryScope } from './types';

/**
 * Pick the memory scope that is CONSISTENT with the scopes the sub-agent was actually granted.
 *
 * Memory is configured on two independent surfaces that can drift: xAgent's `memory_scope` and
 * Auth's `allowed_scopes`. The Memory service authorizes writes against the FORWARDED AGENT JWT,
 * so `memory_scope: 'agent'` WITHOUT the `mem:write` scope is a permanently broken pair — every
 * task burns a guaranteed 403, silently (the MEMORY_WRITE stage is fail-soft). Deriving one from
 * the other makes that combination unrepresentable.
 */
export function memoryScopeFor(grantedScopes: readonly string[]): MemoryScope {
  return grantedScopes.includes('mem:write') ? 'agent' : 'none';
}

/**
 * A complete AgentRuntimeRegistration: every required field defaulted, `status` pinned to 'active'
 * so the sub-agent is immediately roster-eligible (the roster filters on status='active').
 *
 * `description` is deliberately NOT defaulted. It is what the orchestrator's planner routes on, and
 * a made-up default would be worse than none: every sub-agent would advertise the same purpose and
 * the planner would be back to guessing from the agent's name (which is exactly how it used to hand
 * a GitHub lookup to a Wikipedia-only agent). The creation UI requires the caller to supply it.
 */
export function subAgentRegistration(
  overrides: Partial<AgentRuntimeRegistration> & { name: string },
): AgentRuntimeRegistration {
  return {
    status: 'active',
    llm_model: 'smart',
    system_prompt: 'You are a helpful assistant. Answer concisely.',
    max_tokens: 2048,
    temperature: 0.7,
    memory_scope: 'none', // callers pass memoryScopeFor(grantedScopes) — see above
    guardrail_policy_id: null,
    allowed_tools: [],
    tool_loop_enabled: true,
    allowed_skills: [],
    allowed_kb_ids: [],
    rag_top_k_per_kb: 5,
    rag_min_score: 0.7,
    token_budget_per_task: 10000,
    ...overrides,
  };
}

/**
 * Project a live runtime row back onto a PUT body, applying `overrides`.
 *
 * The PUT model is `extra=forbid`, so AgentRuntime's read-only fields (agent_id, tenant_id,
 * runtime_version, capabilities, metadata) must be DROPPED — spreading the row would 422.
 * Used to change one field (e.g. status) without clobbering the agent's other settings.
 */
export function runtimeToRegistration(
  rt: AgentRuntime,
  overrides: Partial<AgentRuntimeRegistration> = {},
): AgentRuntimeRegistration {
  return {
    name: rt.name,
    status: rt.status,
    description: rt.description ?? '',
    llm_model: rt.llm_model,
    system_prompt: rt.system_prompt,
    max_tokens: rt.max_tokens,
    temperature: rt.temperature,
    memory_scope: rt.memory_scope,
    guardrail_policy_id: rt.guardrail_policy_id,
    allowed_tools: rt.allowed_tools,
    tool_loop_enabled: rt.tool_loop_enabled,
    allowed_skills: rt.allowed_skills,
    allowed_kb_ids: rt.allowed_kb_ids,
    rag_top_k_per_kb: rt.rag_top_k_per_kb,
    rag_min_score: rt.rag_min_score,
    token_budget_per_task: rt.token_budget_per_task,
    ...overrides,
  };
}
