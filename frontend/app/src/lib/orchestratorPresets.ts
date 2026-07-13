/**
 * Default orchestrator roster presets — the researcher / writer / reviewer sub-agents an
 * orchestration run needs on a FRESH tenant (else every run fails UNASSIGNED_NODE).
 *
 * Two hard invariants, both verified against the xAgent orchestration engine:
 *   1. `name` MUST be exactly the preset string. The driver binds a DAG node to a sub-agent
 *      BY NAME (orchestration/service roster = {r.name: r.agent_id}; driver resolves
 *      node.preset in roster). decompose.py emits presets researcher|writer|reviewer.
 *   2. `status` MUST be 'active'. The roster query filters xagent.agents on status='active';
 *      the PUT /runtime create-path inserts status straight from the body (no forced
 *      pending_config), so we register active in one shot — no per-agent publish step.
 *
 * Scopes are the DESIRED least-privilege set per role; the seed flow intersects them with the
 * orchestrator's own scopes at runtime so the subset rule (sub ⊆ orchestrator) can never 403.
 */
import {
  createSubAgent,
  getRuntime,
  listSubAgents,
  putRuntime,
  setToolAccess,
  type SubAgent,
} from './services';
import type { AgentRuntimeRegistration } from './types';

export type PresetName = 'researcher' | 'writer' | 'reviewer';

export interface SubAgentPreset {
  /** MUST equal the xAgent decompose preset name — the roster binds node.preset → sub-agent by NAME. */
  name: PresetName;
  /** Human label for the seed UI (never sent to the API). */
  label: string;
  /** Desired scopes (⊆ orchestrator's); intersected with the live orchestrator scopes on seed. */
  desiredScopes: string[];
  /** The exact putRuntime body — every required field present, status already 'active'. */
  registration: AgentRuntimeRegistration;
}

/** All required AgentRuntimeRegistration defaults, with status pinned to 'active' so a seeded
 *  sub-agent is immediately roster-eligible. */
function reg(overrides: Partial<AgentRuntimeRegistration> & { name: PresetName }): AgentRuntimeRegistration {
  return {
    // `name` is supplied by ...overrides (required there); every other field below is a default
    // the caller may override. status is pinned 'active' so a seeded sub-agent is roster-eligible.
    status: 'active',
    llm_model: 'smart',
    system_prompt: 'You are a helpful assistant. Answer concisely.',
    max_tokens: 2048,
    temperature: 0.7,
    memory_scope: 'agent',
    guardrail_policy_id: null,
    allowed_tools: [],
    tool_loop_enabled: true,
    allowed_skills: [],
    allowed_kb_ids: [], // stay empty on a fresh tenant — no KBs exist yet; RAG stage self-skips
    rag_top_k_per_kb: 5,
    rag_min_score: 0.7,
    token_budget_per_task: 10000,
    ...overrides,
  };
}

export const DEFAULT_SUBAGENT_PRESETS: SubAgentPreset[] = [
  {
    name: 'researcher',
    label: 'Researcher',
    desiredScopes: ['agent:execute', 'llm:invoke', 'guardrails:check', 'rag:query', 'tool:invoke', 'mem:read', 'mem:write'],
    registration: reg({
      name: 'researcher',
      llm_model: 'smart',
      temperature: 0.3,
      allowed_tools: ['web_search'], // platform tool; if absent the tool-loop self-skips (fail-soft)
      tool_loop_enabled: true,
      token_budget_per_task: 12000,
      system_prompt:
        'You are the RESEARCHER sub-agent in an orchestrated workflow. Given a goal or sub-task, ' +
        'gather the most relevant facts, sources, and findings. Use available tools (e.g. web_search) ' +
        'and knowledge bases when helpful. Return a compact, well-structured findings brief with ' +
        'concrete evidence and citations. Do NOT write the final prose answer — that is the writer’s job.',
    }),
  },
  {
    name: 'writer',
    label: 'Writer',
    desiredScopes: ['agent:execute', 'llm:invoke', 'guardrails:check', 'mem:read', 'mem:write'],
    registration: reg({
      name: 'writer',
      llm_model: 'smart',
      temperature: 0.7,
      max_tokens: 3072,
      allowed_tools: [],
      tool_loop_enabled: false, // pure drafting → single LLM call
      token_budget_per_task: 10000,
      system_prompt:
        'You are the WRITER sub-agent in an orchestrated workflow. You receive the researcher’s ' +
        'findings as context. Draft a clear, well-organized answer that fully addresses the goal, ' +
        'grounded strictly in the supplied findings. Prefer accuracy over embellishment; do not invent ' +
        'facts. Output the draft only.',
    }),
  },
  {
    name: 'reviewer',
    label: 'Reviewer',
    desiredScopes: ['agent:execute', 'llm:invoke', 'guardrails:check', 'mem:read'],
    registration: reg({
      name: 'reviewer',
      llm_model: 'fast', // a lighter critique pass
      temperature: 0.2,
      memory_scope: 'none', // stateless review
      allowed_tools: [],
      tool_loop_enabled: false,
      token_budget_per_task: 6000,
      system_prompt:
        'You are the REVIEWER sub-agent in an orchestrated workflow. You receive the writer’s draft. ' +
        'Critically review it for accuracy, completeness, clarity, and alignment with the goal, then ' +
        'return a corrected, polished final version. Fix errors and tighten prose; flag any unsupported ' +
        'claims you cannot verify.',
    }),
  },
];

export interface SeedOutcome {
  preset: PresetName;
  action: 'created' | 'runtime-registered' | 'already-present' | 'failed';
  agentId?: string;
  error?: unknown;
}

/** Page through every existing sub-agent so idempotency covers a paginated tenant. */
async function existingByName(signal?: AbortSignal): Promise<Map<string, SubAgent>> {
  const byName = new Map<string, SubAgent>();
  let cursor: string | undefined;
  do {
    const page = await listSubAgents({ cursor, limit: 100 }, signal);
    for (const a of page.items) byName.set(a.name, a);
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  return byName;
}

/** Ensure the xAgent runtime row exists for a sub-agent (self-heals a half-seeded agent whose
 *  identity was created but whose putRuntime never landed). getRuntime 404s ⇒ register. */
async function ensureRuntime(agentId: string, registration: AgentRuntimeRegistration): Promise<boolean> {
  try {
    await getRuntime(agentId);
    return false; // runtime already present — leave it untouched
  } catch {
    await putRuntime(agentId, registration); // PUT create-path inserts status='active' as supplied
    return true;
  }
}

/**
 * Seed researcher / writer / reviewer for the calling orchestrator. Idempotent:
 *   - a sub-agent whose NAME already exists is never recreated (skip the identity step);
 *   - the runtime is ensured regardless, so a partially-seeded agent is healed on re-run.
 * Scopes are intersected with the orchestrator's live scopes so the subset rule can't 403.
 */
export async function seedDefaultSubAgents(orchestratorScopes: readonly string[]): Promise<SeedOutcome[]> {
  const orchScopeSet = new Set(orchestratorScopes);
  const byName = await existingByName();
  const outcomes: SeedOutcome[] = [];

  for (const preset of DEFAULT_SUBAGENT_PRESETS) {
    try {
      const existing = byName.get(preset.name);
      let agentId: string;
      let created = false;

      if (existing) {
        agentId = existing.agent_id;
      } else {
        const scopes = preset.desiredScopes.filter((s) => orchScopeSet.has(s)); // guaranteed ⊆ orchestrator
        const sub = await createSubAgent({ name: preset.name, allowed_scopes: scopes });
        agentId = sub.agent_id;
        created = true;
      }

      const registeredNow = await ensureRuntime(agentId, preset.registration);

      // Best-effort: let the researcher actually use web_search. Non-fatal — the tool-loop self-skips
      // a tool the agent can't access, so a fresh tenant without web_search still seeds cleanly.
      for (const tool of preset.registration.allowed_tools) {
        try {
          await setToolAccess(tool, { agent_id: agentId, access_mode: 'automated' });
        } catch {
          /* tool not present / no grant capability — ignore */
        }
      }

      outcomes.push({
        preset: preset.name,
        agentId,
        action: created ? 'created' : registeredNow ? 'runtime-registered' : 'already-present',
      });
    } catch (error) {
      outcomes.push({ preset: preset.name, action: 'failed', error });
    }
  }
  return outcomes;
}
