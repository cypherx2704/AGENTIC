/**
 * Typed service functions — thin, named wrappers over the BFF proxy so screens never
 * build raw paths. Every call routes through `api(service, path)` in bff-client, which
 * enforces credentials + CSRF + Contract-2 error normalization.
 */

import { api } from './bff-client';
import type {
  Agent,
  AgentRuntime,
  AgentRuntimeRegistration,
  ApiKeyListItem,
  AuditListResponse,
  AuditVerifyResult,
  CostRow,
  CreateKeyResponse,
  GroupedResult,
  KbListResponse,
  LlmModel,
  Policy,
  PolicyListResponse,
  TaskListResponse,
  TaskResponse,
  UsageRow,
  ViolationListResponse,
} from './types';

// ── Auth: agents ──────────────────────────────────────────────────────────────────────
/**
 * Auth `GET /v1/agents` returns the Contract-9 cursor shape `{ items, next_cursor }`. We keep the
 * legacy `agents`/`data` keys in the union as a tolerant fallback (older/alternate gateways), but
 * `items` is the authoritative field. `next_cursor` is null on the last page.
 */
export interface AgentListResponse {
  items?: Agent[];
  agents?: Agent[];
  data?: Agent[];
  next_cursor?: string | null;
}

/** Parameters for {@link listAgents} (all optional; map 1:1 to the auth query params). */
export interface ListAgentsParams {
  cursor?: string;
  limit?: number;
  status?: string;
  name?: string;
}

/** List agents in the caller's tenant (cursor-paginated). Pass `cursor` to fetch the next page. */
export function listAgents(params: ListAgentsParams = {}, signal?: AbortSignal): Promise<AgentListResponse> {
  return api<AgentListResponse>('auth', '/v1/agents', { query: { ...params }, signal });
}

export function getAgent(agentId: string, signal?: AbortSignal): Promise<Agent> {
  return api<Agent>('auth', `/v1/agents/${agentId}`, { signal });
}

export function createAgent(body: {
  name: string;
  version?: string;
  allowed_scopes?: string[];
}): Promise<Agent> {
  return api<Agent>('auth', '/v1/agents', { method: 'POST', body });
}

// ── Auth: API keys ──────────────────────────────────────────────────────────────────────
export interface KeyListResponse {
  keys: ApiKeyListItem[];
}

export function listKeys(agentId: string, signal?: AbortSignal): Promise<KeyListResponse> {
  return api<KeyListResponse>('auth', `/v1/agents/${agentId}/keys`, { signal });
}

export function createKey(
  agentId: string,
  body: { scopes: string[]; name?: string; expires_in_days?: number },
): Promise<CreateKeyResponse> {
  return api<CreateKeyResponse>('auth', `/v1/agents/${agentId}/keys`, { method: 'POST', body });
}

export function revokeKey(agentId: string, keyId: string): Promise<void> {
  return api<void>('auth', `/v1/agents/${agentId}/keys/${keyId}`, { method: 'DELETE' });
}

// ── Auth: audit log ─────────────────────────────────────────────────────────────────────
export function listAuditLog(
  params: { from?: string; to?: string; event_type?: string; agent_id?: string; cursor?: string; limit?: number },
  signal?: AbortSignal,
): Promise<AuditListResponse> {
  return api<AuditListResponse>('auth', '/v1/audit-log', { query: params, signal });
}

export function verifyAuditChain(
  params: { from?: string; to?: string },
  signal?: AbortSignal,
): Promise<AuditVerifyResult> {
  return api<AuditVerifyResult>('auth', '/v1/audit-log/verify', { query: params, signal });
}

// ── Auth: tenant self-service quotas (Contract-19 effective limits) ───────────────────────
/**
 * The caller tenant's effective per-service limits doc (deep-merge of plan defaults + any
 * override): one block per service — `auth`, `llms`, `rag`, `memory`, `tools`. Requires
 * `tenant:read` / `tenant:admin` / `platform:admin` (Auth `GET /v1/quotas`). Rendered
 * shape-tolerantly so a new service block / limit key appears without a UI change.
 */
export function getMyQuotas(signal?: AbortSignal): Promise<Record<string, Record<string, number>>> {
  return api<Record<string, Record<string, number>>>('auth', '/v1/quotas', { signal });
}

// ── xAgent: runtime config ──────────────────────────────────────────────────────────────
export function getRuntime(agentId: string, signal?: AbortSignal): Promise<AgentRuntime> {
  return api<AgentRuntime>('xagent', `/v1/agents/${agentId}/runtime`, { signal });
}

/** PUT upserts (create on first write, transition+bump thereafter) — the Agent Builder save. */
export function putRuntime(agentId: string, body: AgentRuntimeRegistration): Promise<AgentRuntime> {
  return api<AgentRuntime>('xagent', `/v1/agents/${agentId}/runtime`, { method: 'PUT', body });
}

// ── xAgent: tasks ───────────────────────────────────────────────────────────────────────
export interface SubmitTaskBody {
  agent_id: string;
  input: { message: string; [k: string]: unknown };
  metadata?: Record<string, unknown>;
  session_id?: string;
  timeout_seconds?: number;
}

export function submitTask(body: SubmitTaskBody): Promise<TaskResponse> {
  return api<TaskResponse>('xagent', '/v1/tasks', { method: 'POST', body });
}

export function getTask(taskId: string, signal?: AbortSignal): Promise<TaskResponse> {
  return api<TaskResponse>('xagent', `/v1/tasks/${taskId}`, { signal });
}

export function listTasks(
  params: { status?: string; agent_id?: string; since?: string; cursor?: string; limit?: number },
  signal?: AbortSignal,
): Promise<TaskListResponse> {
  return api<TaskListResponse>('xagent', '/v1/tasks', { query: params, signal });
}

// ── LLMs: models / usage / cost ─────────────────────────────────────────────────────────
export function listModels(signal?: AbortSignal): Promise<{ data: LlmModel[] }> {
  return api<{ data: LlmModel[] }>('llms', '/v1/models', { signal });
}

export function getUsage(
  params: { from?: string; to?: string; group_by?: string },
  signal?: AbortSignal,
): Promise<GroupedResult<UsageRow>> {
  return api<GroupedResult<UsageRow>>('llms', '/v1/usage', { query: params, signal });
}

export function getCost(
  params: { from?: string; to?: string; group_by?: string },
  signal?: AbortSignal,
): Promise<GroupedResult<CostRow>> {
  return api<GroupedResult<CostRow>>('llms', '/v1/cost', { query: params, signal });
}

// ── Guardrails: policies + violations ──────────────────────────────────────────────────
export function listPolicies(signal?: AbortSignal): Promise<PolicyListResponse> {
  return api<PolicyListResponse>('guardrails', '/v1/policies', { signal });
}

export function getPolicy(policyId: string, signal?: AbortSignal): Promise<Policy> {
  return api<Policy>('guardrails', `/v1/policies/${policyId}`, { signal });
}

export function createPolicy(body: {
  name: string;
  rules: Array<{ rule_id: string; enabled: boolean; action_override?: string | null }>;
  stream_mode?: string;
  fail_mode_override?: string | null;
}): Promise<Policy> {
  return api<Policy>('guardrails', '/v1/policies', { method: 'POST', body });
}

export function editPolicy(
  policyId: string,
  body: {
    name: string;
    rules: Array<{ rule_id: string; enabled: boolean; action_override?: string | null }>;
    stream_mode?: string;
    fail_mode_override?: string | null;
  },
): Promise<Policy> {
  return api<Policy>('guardrails', `/v1/policies/${policyId}`, { method: 'PUT', body });
}

export function listViolations(
  params: { from?: string; to?: string; agent_id?: string; decision?: string; limit?: number; after_id?: string },
  signal?: AbortSignal,
): Promise<ViolationListResponse> {
  return api<ViolationListResponse>('guardrails', '/v1/violations', { query: params, signal });
}

// ── RAG: knowledge bases ────────────────────────────────────────────────────────────────
export function listKnowledgeBases(signal?: AbortSignal): Promise<KbListResponse> {
  return api<KbListResponse>('rag', '/v1/kbs', { signal });
}

export interface KbQueryResult {
  results?: Array<{ chunk_id?: string; score?: number; text?: string; [k: string]: unknown }>;
  data?: Array<Record<string, unknown>>;
  [k: string]: unknown;
}

export function queryKnowledgeBase(
  kbId: string,
  body: { query: string; top_k?: number; min_score?: number },
): Promise<KbQueryResult> {
  return api<KbQueryResult>('rag', `/v1/kbs/${kbId}/query`, { method: 'POST', body });
}

// ── LLM provider connections (BYOK — keys stored AES-encrypted in the DB, per tenant) ──
// The raw secret is sent once on create and never returned; list/delete carry no secret.
export interface LlmConnection {
  key_id: string;
  provider: string;
  priority: number;
  status: string;
  base_url?: string | null;
  kind?: string | null;
  label?: string | null;
  grace_until?: string | null;
}

export interface LlmConnectionListResponse {
  data?: LlmConnection[];
  keys?: LlmConnection[];
  [k: string]: unknown;
}

export interface CreateLlmConnectionBody {
  provider: string;
  secret: string;
  base_url?: string | null;
  kind?: string | null;
  label?: string | null;
  priority?: number;
}

export function listLlmConnections(signal?: AbortSignal): Promise<LlmConnectionListResponse> {
  return api<LlmConnectionListResponse>('llms', '/v1/keys', { signal });
}

export function createLlmConnection(body: CreateLlmConnectionBody): Promise<LlmConnection> {
  return api<LlmConnection>('llms', '/v1/keys', { method: 'POST', body });
}

export function deleteLlmConnection(keyId: string): Promise<void> {
  return api<void>('llms', `/v1/keys/${keyId}`, { method: 'DELETE' });
}

// ── Orchestrator: sub-agents (orchestrator-only creation/management) ───────────────────────
export interface SubAgent {
  agent_id: string;
  tenant_id: string;
  name: string;
  version: string;
  status: string;
  agent_type: string;
  parent_orchestrator_id?: string | null;
  immutable_llm: boolean;
  allowed_scopes: string[];
}

export function listSubAgents(
  params: { cursor?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<{ items: SubAgent[]; next_cursor?: string | null }> {
  return api('auth', '/v1/orchestrator/sub-agents', { query: { ...params }, signal });
}

export function createSubAgent(body: {
  name: string;
  version?: string;
  allowed_scopes: string[];
}): Promise<SubAgent> {
  return api<SubAgent>('auth', '/v1/orchestrator/sub-agents', { method: 'POST', body });
}

export function updateSubAgent(
  agentId: string,
  body: { allowed_scopes?: string[]; capabilities?: unknown; metadata?: unknown },
): Promise<SubAgent> {
  return api<SubAgent>('auth', `/v1/orchestrator/sub-agents/${agentId}`, { method: 'PATCH', body });
}

export function deactivateSubAgent(agentId: string): Promise<unknown> {
  return api('auth', `/v1/orchestrator/sub-agents/${agentId}`, { method: 'DELETE' });
}

// ── HIL: approvals + orchestrator mode config ──────────────────────────────────────────────
export interface HilApproval {
  request_id: string;
  agent_id: string;
  operation_type?: string;
  context: Record<string, unknown>;
  status: string;
  requested_at: string;
  expires_at: string;
  resolved_at?: string | null;
}

export function listHilApprovals(
  params: { operation_type?: string } = {},
  signal?: AbortSignal,
): Promise<{ items: HilApproval[] }> {
  return api('auth', '/v1/hil/approvals', { query: { ...params }, signal });
}

export function grantHilApproval(requestId: string, note?: string): Promise<HilApproval> {
  return api<HilApproval>('auth', `/v1/hil/approvals/${requestId}/grant`, {
    method: 'POST',
    body: { note },
  });
}

export function denyHilApproval(requestId: string, note?: string): Promise<HilApproval> {
  return api<HilApproval>('auth', `/v1/hil/approvals/${requestId}/deny`, {
    method: 'POST',
    body: { note },
  });
}

export interface HilConfig {
  agent_id: string;
  default_mode: string; // automated | human_in_loop | partial
  ask_on_triggers: string[];
}

export function getHilConfig(signal?: AbortSignal): Promise<HilConfig> {
  return api<HilConfig>('auth', '/v1/orchestrator/hil-config', { signal });
}

export function putHilConfig(body: {
  default_mode: string;
  ask_on_triggers?: string[];
}): Promise<HilConfig> {
  return api<HilConfig>('auth', '/v1/orchestrator/hil-config', { method: 'PUT', body });
}

// ── LLMs: alias governance (is_default / task_type) + per-agent allowlist + user rules ──────
export interface LlmAlias {
  id: string;
  tenant_id?: string | null;
  alias: string;
  model_id: string;
  provider: string;
  is_default: boolean;
  task_type?: string | null;
  description?: string | null;
}

export function listAliases(
  params: { task_type?: string } = {},
  signal?: AbortSignal,
): Promise<{ data: LlmAlias[] }> {
  return api('llms', '/v1/models/aliases', { query: { ...params }, signal });
}

export function createAlias(body: {
  alias: string;
  model_id: string;
  provider: string;
  task_type?: string;
  description?: string;
  is_default?: boolean;
}): Promise<LlmAlias> {
  return api<LlmAlias>('llms', '/v1/models/aliases', { method: 'POST', body });
}

export function updateAlias(
  alias: string,
  body: { is_default?: boolean; task_type?: string; description?: string },
): Promise<LlmAlias> {
  return api<LlmAlias>('llms', `/v1/models/aliases/${alias}`, { method: 'PATCH', body });
}

export function deleteAlias(alias: string): Promise<void> {
  return api<void>('llms', `/v1/models/aliases/${alias}`, { method: 'DELETE' });
}

export function getAgentLlmAliases(agentId: string, signal?: AbortSignal): Promise<{ aliases: string[] }> {
  return api('llms', `/v1/agents/${agentId}/llm-aliases`, { signal });
}

export function putAgentLlmAliases(agentId: string, aliases: string[]): Promise<{ aliases: string[] }> {
  return api('llms', `/v1/agents/${agentId}/llm-aliases`, { method: 'PUT', body: { aliases } });
}

export interface LlmRule {
  rule_id: string;
  provider: string;
  model_id: string;
  rule_type: string; // allow | block
  can_be_used_by_agents: boolean;
  is_user_added: boolean;
  billing_bypass: boolean;
}

export function listLlmRules(signal?: AbortSignal): Promise<{ data: LlmRule[] }> {
  return api('llms', '/v1/llm-rules', { signal });
}

export function createLlmRule(body: {
  provider: string;
  model_id: string;
  rule_type?: string;
  can_be_used_by_agents?: boolean;
  billing_bypass?: boolean;
  is_user_added?: boolean;
}): Promise<LlmRule> {
  return api<LlmRule>('llms', '/v1/llm-rules', { method: 'POST', body });
}

export function deleteLlmRule(ruleId: string): Promise<void> {
  return api<void>('llms', `/v1/llm-rules/${ruleId}`, { method: 'DELETE' });
}

// ── Tools: per-agent access control ──────────────────────────────────────────────────────
export function getToolAccess(
  toolName: string,
  params: { agent_id?: string; capability?: string } = {},
  signal?: AbortSignal,
): Promise<{ tool: string; agent_id: string; access_mode: string; restricted: boolean }> {
  return api('tools', `/v1/tools/${toolName}/access`, { query: { ...params }, signal });
}

export function setToolAccess(
  toolName: string,
  body: { agent_id: string; access_mode: string; capability?: string },
): Promise<unknown> {
  return api('tools', `/v1/tools/${toolName}/access`, { method: 'PUT', body });
}

export function listRestrictedTools(signal?: AbortSignal): Promise<{ data: Array<Record<string, unknown>> }> {
  return api('tools', '/v1/restricted-tools', { signal });
}
