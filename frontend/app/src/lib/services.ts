/**
 * Typed service functions — thin, named wrappers over the BFF proxy so screens never
 * build raw paths. Every call routes through `api(service, path)` in bff-client, which
 * enforces credentials + CSRF + Contract-2 error normalization.
 */

import { api, streamUrl } from './bff-client';
import type {
  AccessMode,
  AccessResolution,
  Agent,
  ServiceClientView,
  SigningKeyView,
  TenantView,
  Webhook,
  WebhookDelivery,
  AgentRuntime,
  AgentRuntimeRegistration,
  ApiKeyListItem,
  AuditListResponse,
  AuditVerifyResult,
  CheckResult,
  CostRow,
  CreateKeyResponse,
  CustomRule,
  CustomRuleInput,
  DeactivateAgentResult,
  GdprWipeResult,
  GroupedResult,
  KbDetail,
  KbListResponse,
  KbStatus,
  KnowledgeBase,
  LlmModel,
  MemoryRecord,
  MemorySearchResponse,
  MemorySession,
  MemoryVisibility,
  Policy,
  PolicyListResponse,
  RagAcl,
  RagDocument,
  RagDocumentListResponse,
  RagQueryResponse,
  RagSearchMode,
  RagUploadUrl,
  RotateKeyResponse,
  SimulationResult,
  SkillView,
  TaskListResponse,
  TaskResponse,
  TaskStep,
  ToolView,
  ToolVisibility,
  FlowTool,
  PublishFlowToolRequest,
  PublishFlowToolResult,
  BridgeTool,
  CreateBridgeToolRequest,
  CreateBridgeToolResult,
  Mcp,
  CreateMcpRequest,
  UpdateMcpRequest,
  UnpublishMcpResult,
  EditorSession,
  NoderedFlow,
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

/**
 * Cancel a pending/running task (`DELETE /v1/tasks/{id}`). Terminal tasks are a no-op/409.
 * Returns the task's post-cancel state when the gateway echoes it (else resolves void).
 */
export function cancelTask(taskId: string): Promise<TaskResponse | void> {
  return api<TaskResponse | void>('xagent', `/v1/tasks/${taskId}`, { method: 'DELETE' });
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
/**
 * List KBs in the tenant. The RAG service returns a **bare array** (`KbResponse[]`); older/
 * alternate gateways wrapped it as `{data|knowledge_bases|items|kbs}`. Normalize every shape
 * to a plain `KnowledgeBase[]` so callers never have to unwrap.
 */
export async function listKnowledgeBases(signal?: AbortSignal): Promise<KnowledgeBase[]> {
  const raw = await api<KbListResponse | KnowledgeBase[]>('rag', '/v1/kbs', { signal });
  if (Array.isArray(raw)) return raw as KnowledgeBase[];
  const r = raw as KbListResponse & { items?: KnowledgeBase[]; kbs?: KnowledgeBase[] };
  return r.data ?? r.knowledge_bases ?? r.items ?? r.kbs ?? [];
}

export function getKnowledgeBase(kbId: string, signal?: AbortSignal): Promise<KbDetail> {
  return api<KbDetail>('rag', `/v1/kbs/${kbId}`, { signal });
}

/** KB rollup counts (document/chunk totals, pending/failed) — enriches the KB list + detail. */
export function getKbStatus(kbId: string, signal?: AbortSignal): Promise<KbStatus> {
  return api<KbStatus>('rag', `/v1/kbs/${kbId}/status`, { signal });
}

export function createKnowledgeBase(body: {
  name: string;
  description?: string | null;
  chunking_strategy?: 'fixed' | 'sentence';
  chunk_size?: number;
  chunk_overlap?: number;
  embedding_model_alias?: string;
  private?: boolean;
}): Promise<KbDetail> {
  return api<KbDetail>('rag', '/v1/kbs', { method: 'POST', body });
}

export function deleteKnowledgeBase(kbId: string): Promise<void> {
  return api<void>('rag', `/v1/kbs/${kbId}`, { method: 'DELETE' });
}

/** Retrieval query with the full flag matrix (search_mode/min_score/rerank/filters are opt-in). */
export function queryKnowledgeBase(
  kbId: string,
  body: {
    query: string;
    top_k?: number;
    min_score?: number;
    filters?: Record<string, unknown> | null;
    search_mode?: RagSearchMode;
    ef_search?: number;
    rerank?: boolean;
    decompose?: boolean;
    multi_query?: boolean;
  },
): Promise<RagQueryResponse> {
  return api<RagQueryResponse>('rag', `/v1/kbs/${kbId}/query`, { method: 'POST', body });
}

// ── RAG: documents (ingest lifecycle) ─────────────────────────────────────────────────────
/** Inline ingest: paste ≤100 KiB of markdown/text directly (no object store round-trip). */
export function inlineIngest(
  kbId: string,
  body: { name: string; content: string; source_type?: 'markdown' | 'text'; metadata?: Record<string, unknown> },
): Promise<RagDocument> {
  return api<RagDocument>('rag', `/v1/kbs/${kbId}/documents`, { method: 'POST', body });
}

/** Step 1 of file upload: get a presigned PUT URL + the doc_id to finalize with. */
export function requestUploadUrl(
  kbId: string,
  body: { filename: string; size_bytes: number; content_type: string },
): Promise<RagUploadUrl> {
  return api<RagUploadUrl>('rag', `/v1/kbs/${kbId}/documents/upload-url`, { method: 'POST', body });
}

/**
 * Step 3 of file upload: after the browser PUTs the bytes to the presigned URL, finalize
 * enqueues ingestion. Idempotent on `doc_id` (Contract-9) — pass an Idempotency-Key so a
 * retried finalize never double-enqueues.
 */
export function finalizeDocument(kbId: string, docId: string, idempotencyKey?: string): Promise<RagDocument> {
  return api<RagDocument>('rag', `/v1/kbs/${kbId}/documents/finalize`, {
    method: 'POST',
    body: { doc_id: docId },
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
  });
}

export function listDocuments(
  kbId: string,
  params: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<RagDocumentListResponse> {
  return api<RagDocumentListResponse>('rag', `/v1/kbs/${kbId}/documents`, { query: { ...params }, signal });
}

export function getDocument(kbId: string, docId: string, signal?: AbortSignal): Promise<RagDocument> {
  return api<RagDocument>('rag', `/v1/kbs/${kbId}/documents/${docId}`, { signal });
}

export function deleteDocument(kbId: string, docId: string): Promise<void> {
  return api<void>('rag', `/v1/kbs/${kbId}/documents/${docId}`, { method: 'DELETE' });
}

// ── RAG: KB ACLs (who can read/query/ingest/write/admin a KB) ──────────────────────────────
export function listKbAcls(kbId: string, signal?: AbortSignal): Promise<{ acls: RagAcl[] }> {
  return api<{ acls: RagAcl[] }>('rag', `/v1/kbs/${kbId}/acls`, { signal });
}

export function addKbAcl(kbId: string, acl: RagAcl): Promise<RagAcl> {
  return api<RagAcl>('rag', `/v1/kbs/${kbId}/acls`, { method: 'POST', body: acl });
}

export function replaceKbAcls(kbId: string, acls: RagAcl[]): Promise<{ acls: RagAcl[] }> {
  return api<{ acls: RagAcl[] }>('rag', `/v1/kbs/${kbId}/acls`, { method: 'PUT', body: { acls } });
}

export function deleteKbAcl(kbId: string, principalType: string, principalId: string): Promise<void> {
  return api<void>('rag', `/v1/kbs/${kbId}/acls/${principalType}/${encodeURIComponent(principalId)}`, {
    method: 'DELETE',
  });
}

// ── Memory service (principal-scoped agent memory) ─────────────────────────────────────────
/** Vector search over the caller's memories (+ tenant_shared when include_shared). */
export function searchMemories(
  body: {
    query: string;
    top_k?: number;
    type?: string | null;
    tags?: string[] | null;
    include_shared?: boolean;
    session_scope_id?: string | null;
    agent_scope_id?: string | null;
  },
  signal?: AbortSignal,
): Promise<MemorySearchResponse> {
  return api<MemorySearchResponse>('memory', '/v1/memories/search', { method: 'POST', body, signal });
}

/**
 * Store a memory. Idempotency-Key short-circuits BEFORE embedding server-side, so pass one
 * to make a retried store safe (never re-embeds, never double-inserts).
 */
export function storeMemory(
  body: {
    content: string;
    type?: string;
    scope?: MemoryVisibility;
    tags?: string[];
    metadata?: Record<string, unknown>;
    session_id?: string | null;
    ttl_seconds?: number | null;
    importance?: number | null;
  },
  idempotencyKey?: string,
): Promise<MemoryRecord> {
  return api<MemoryRecord>('memory', '/v1/memories', {
    method: 'POST',
    body,
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
  });
}

export function getMemory(id: string, signal?: AbortSignal): Promise<MemoryRecord> {
  return api<MemoryRecord>('memory', `/v1/memories/${id}`, { signal });
}

export function updateMemory(
  id: string,
  body: {
    content?: string;
    scope?: MemoryVisibility;
    tags?: string[];
    metadata?: Record<string, unknown>;
    ttl_seconds?: number | null;
  },
): Promise<MemoryRecord> {
  return api<MemoryRecord>('memory', `/v1/memories/${id}`, { method: 'PUT', body });
}

export function deleteMemory(id: string): Promise<void> {
  return api<void>('memory', `/v1/memories/${id}`, { method: 'DELETE' });
}

export function createMemorySession(body: {
  session_id: string;
  title?: string | null;
  metadata?: Record<string, unknown>;
}): Promise<MemorySession> {
  return api<MemorySession>('memory', '/v1/sessions', { method: 'POST', body });
}

/**
 * GDPR right-to-erasure: wipe every memory for a principal. Defaults to the caller's own
 * principal; an admin (mem:write) may target another principal in the same tenant by id.
 */
export function gdprWipeMemories(body: {
  principal_type?: string | null;
  principal_id?: string | null;
  reason?: string | null;
}): Promise<GdprWipeResult> {
  return api<GdprWipeResult>('memory', '/v1/gdpr/wipe', { method: 'POST', body });
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

// ── Orchestrations (PROMPT → ORCHESTRATOR → SUB-AGENTS runs) ────────────────────────────────
// These live on the xAgent service (POST /v1/orchestrations …); the generic BFF proxy routes them,
// and the run SSE stream is relayed unbuffered (isStreamRoute covers /v1/orchestrations/{id}/stream).
export interface OrchestrationRun {
  workflow_id: string;
  tenant_id?: string;
  root_agent_id?: string;
  goal: string;
  status: string; // pending | planning | running | awaiting_approval | completed | failed | cancelled | timeout
  mode: string; // subagents | solo
  decomposition?: string | null; // template | llm
  output?: { message?: string } | null;
  error_code?: string | null;
  error_msg?: string | null;
  tokens_used?: number | null;
  cost_usd?: number | null;
  cost_budget_usd?: number | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface OrchestrationNode {
  node_id: string;
  node_type: string;
  status: string;
  assigned_agent_id?: string | null;
  preset?: string | null;
  depends_on: string[];
  task_id?: string | null;
  output?: { summary?: string; citations?: string[] } | null;
  tokens_used?: number | null;
  cost_usd?: number | null;
  started_at?: string | null;
  completed_at?: string | null;
  /**
   * What this sub-agent actually DID: its own pipeline audit trail (guardrail → llm → tool_call …),
   * the same TaskStep shape the single-agent Task Runner renders. Carried inline on the graph/SSE
   * frames — so the tree streams a sub-agent's TOOL CALLS as they happen, rather than the UI lazily
   * re-fetching each node's task (an N+1 that showed nothing at all while a node was still running).
   */
  steps?: TaskStep[];
}

export interface OrchestrationGraph {
  workflow: OrchestrationRun;
  nodes: OrchestrationNode[];
}

/** Submit a goal to the orchestrator; it decomposes + fans out to sub-agents. Returns 202 + workflow_id.
 *
 * `mode` and `use_tools` are INDEPENDENT switches:
 *   * `mode`      — may the orchestrator delegate to sub-agents, or must it answer alone?
 *   * `use_tools` — may any agent in the run call its tools at all?
 * `mode: 'solo'` + `use_tools: false` is the plain-chatbot configuration: no planner, no roster,
 * no tools. Both default ON server-side, so omitting them preserves today's behaviour.
 */
export function submitOrchestration(body: {
  goal: string;
  mode?: 'subagents' | 'solo';
  use_tools?: boolean;
  cost_budget_usd?: number;
  timeout_seconds?: number;
}): Promise<{ workflow_id: string; status: string; mode: string; use_tools?: boolean; trace_id?: string }> {
  return api('xagent', '/v1/orchestrations', { method: 'POST', body });
}

export function listOrchestrations(
  params: { limit?: number; status?: string } = {},
  signal?: AbortSignal,
): Promise<{ items: OrchestrationRun[] }> {
  return api('xagent', '/v1/orchestrations', { query: { ...params }, signal });
}

export function getOrchestration(workflowId: string, signal?: AbortSignal): Promise<OrchestrationRun> {
  return api<OrchestrationRun>('xagent', `/v1/orchestrations/${workflowId}`, { signal });
}

/** The run + its node tree (the execution graph the UI renders). */
export function getOrchestrationGraph(workflowId: string, signal?: AbortSignal): Promise<OrchestrationGraph> {
  return api<OrchestrationGraph>('xagent', `/v1/orchestrations/${workflowId}/graph`, { signal });
}

/** Signal a run to cancel (terminal runs are a no-op). */
export function cancelOrchestration(workflowId: string): Promise<unknown> {
  return api('xagent', `/v1/orchestrations/${workflowId}`, { method: 'DELETE' });
}

/** Absolute URL for the run's SSE execution-tree stream (used with EventSource + credentials). */
export function orchestrationStreamUrl(workflowId: string): string {
  return streamUrl('xagent', `/v1/orchestrations/${workflowId}/stream`);
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

// ── Tools registry (MCP tool servers) ──────────────────────────────────────────────────────
/** Options for {@link listTools} — currently the optional Marketplace visibility filter. */
export interface ListToolsParams {
  /**
   * Narrow to these Marketplace visibility sections (`public`|`private`|`protected`). A single
   * value or an array (joined comma-separated on the wire, matching the registry's `?visibility=`
   * filter). Omit for all visible tools.
   */
  visibility?: ToolVisibility | ToolVisibility[];
}

/**
 * List tools visible to the tenant (platform + own, tenant-priority shadowed). Pass
 * `{ visibility }` to filter to Marketplace sections via the registry's `?visibility=` filter.
 */
export async function listTools(params: ListToolsParams = {}, signal?: AbortSignal): Promise<ToolView[]> {
  const visibility = Array.isArray(params.visibility) ? params.visibility.join(',') : params.visibility;
  const r = await api<{ data?: ToolView[] } | ToolView[]>('tools', '/v1/tools', {
    query: visibility ? { visibility } : undefined,
    signal,
  });
  return Array.isArray(r) ? r : (r.data ?? []);
}

export function getTool(name: string, version?: string, signal?: AbortSignal): Promise<ToolView> {
  return api<ToolView>('tools', `/v1/tools/${encodeURIComponent(name)}`, {
    query: version ? { version } : undefined,
    signal,
  });
}

/** Register a new tenant tool from a Contract-4 MCP manifest (requires tool:admin). */
export function registerTool(manifest: Record<string, unknown>): Promise<Record<string, unknown>> {
  return api('tools', '/v1/tools', { method: 'POST', body: manifest });
}

// ── Tool Builder (flow-tool-bridge: visual Node-RED flows -> MCP tools) ──────────────────────
/** Ensure the tenant's Node-RED editor is provisioned; returns readiness + the iframe path. */
export function openEditorSession(): Promise<EditorSession> {
  return api<EditorSession>('toolbuilder', '/v1/editor-sessions', { method: 'POST', body: {} });
}

/** List this tenant's published flow-tools. */
export async function listFlowTools(signal?: AbortSignal): Promise<FlowTool[]> {
  const r = await api<{ data?: FlowTool[] } | FlowTool[]>('toolbuilder', '/v1/flow-tools', { signal });
  return Array.isArray(r) ? r : (r.data ?? []);
}

/** Publish (or re-publish) a Node-RED flow as an MCP tool (requires tool:admin). */
export function publishFlowTool(body: PublishFlowToolRequest): Promise<PublishFlowToolResult> {
  return api<PublishFlowToolResult>('toolbuilder', '/v1/flow-tools', { method: 'POST', body });
}

/** Unpublish (retire) a flow-tool. */
export function unpublishFlowTool(slug: string): Promise<{ slug: string; status: string }> {
  return api('toolbuilder', `/v1/flow-tools/${encodeURIComponent(slug)}`, { method: 'DELETE' });
}

/** List the tenant's Node-RED flow tabs (the Publish dialog's workflow picker). */
export async function listNoderedFlows(signal?: AbortSignal): Promise<NoderedFlow[]> {
  const r = await api<{ data?: NoderedFlow[] } | NoderedFlow[]>('toolbuilder', '/v1/flows', { signal });
  return Array.isArray(r) ? r : (r.data ?? []);
}

/** Run a published tool with sample args (owner-only) — the UI's Test action. */
export function testFlowTool(
  slug: string,
  args: Record<string, unknown>,
): Promise<{ tool: string; result: unknown }> {
  return api('toolbuilder', `/v1/flow-tools/${encodeURIComponent(slug)}/test`, {
    method: 'POST',
    body: { args },
  });
}

// ── Tool + MCP workflow (flow-tool-bridge control plane: atomic tools + MCP collections) ──────
// The "atomic tool + aggregating MCP" model. All routes live under the generic `toolbuilder`
// upstream (auto-proxied by the BFF). Scopes are enforced server-side (`tool:admin`, plus
// `tenant:admin` for restricted defaults and `platform:admin` for promote). These wrappers cover
// the whole feature so Tool Builder (4B) + Agent picker (4C) only touch their own page files.

/** Create an atomic tool from a Node-RED flow (`POST /v1/tools`). Requires `tool:admin`.
 *  Without `mcp_ids` the tool gets an auto-created singleton MCP (`tool-<slug>`). */
export function createBridgeTool(body: CreateBridgeToolRequest): Promise<CreateBridgeToolResult> {
  return api<CreateBridgeToolResult>('toolbuilder', '/v1/tools', { method: 'POST', body });
}

/** List this tenant's atomic tools + their MCP memberships (`GET /v1/tools`). */
export async function listBridgeTools(signal?: AbortSignal): Promise<BridgeTool[]> {
  const r = await api<{ data?: BridgeTool[] } | BridgeTool[]>('toolbuilder', '/v1/tools', { signal });
  return Array.isArray(r) ? r : (r.data ?? []);
}

/** Create an MCP collection (`POST /v1/mcps`); every `tool_ids` is ownership-validated + registered. */
export function createMcp(body: CreateMcpRequest): Promise<Mcp> {
  return api<Mcp>('toolbuilder', '/v1/mcps', { method: 'POST', body });
}

/** List this tenant's MCP collections + their member tools (`GET /v1/mcps`). */
export async function listMcps(signal?: AbortSignal): Promise<Mcp[]> {
  const r = await api<{ data?: Mcp[] } | Mcp[]>('toolbuilder', '/v1/mcps', { signal });
  return Array.isArray(r) ? r : (r.data ?? []);
}

/** Update an MCP's metadata/membership (`PUT /v1/mcps/{id}`) → regenerate + re-register (version STABLE). */
export function updateMcp(mcpId: string, body: UpdateMcpRequest): Promise<Mcp> {
  return api<Mcp>('toolbuilder', `/v1/mcps/${encodeURIComponent(mcpId)}`, { method: 'PUT', body });
}

/** (Re)register/refresh an MCP in the registry (`POST /v1/mcps/{id}/publish`). */
export function publishMcp(mcpId: string): Promise<Mcp> {
  return api<Mcp>('toolbuilder', `/v1/mcps/${encodeURIComponent(mcpId)}/publish`, { method: 'POST', body: {} });
}

/** Unpublish (retire) an MCP + its exclusively-owned tools (`DELETE /v1/mcps/{id}`). */
export function unpublishMcp(mcpId: string): Promise<UnpublishMcpResult> {
  return api<UnpublishMcpResult>('toolbuilder', `/v1/mcps/${encodeURIComponent(mcpId)}`, { method: 'DELETE' });
}

/** Promote an MCP to Public — the SOLE path to public (`POST /v1/mcps/{id}/promote`). Requires
 *  `platform:admin`; re-registers under the platform (tenant_id NULL) namespace. */
export function promoteMcp(mcpId: string): Promise<Mcp> {
  return api<Mcp>('toolbuilder', `/v1/mcps/${encodeURIComponent(mcpId)}/promote`, { method: 'POST', body: {} });
}

export function getToolAccess(
  toolName: string,
  params: { agent_id?: string; capability?: string } = {},
  signal?: AbortSignal,
): Promise<AccessResolution> {
  return api<AccessResolution>('tools', `/v1/tools/${encodeURIComponent(toolName)}/access`, {
    query: { ...params },
    signal,
  });
}

export function setToolAccess(
  toolName: string,
  body: { agent_id: string; access_mode: AccessMode; capability?: string },
): Promise<Record<string, unknown>> {
  return api('tools', `/v1/tools/${encodeURIComponent(toolName)}/access`, { method: 'PUT', body });
}

export function listRestrictedTools(signal?: AbortSignal): Promise<{ data: Array<Record<string, unknown>> }> {
  return api('tools', '/v1/restricted-tools', { signal });
}

export function markToolRestricted(toolName: string, reason?: string): Promise<Record<string, unknown>> {
  return api('tools', `/v1/restricted-tools/${encodeURIComponent(toolName)}`, {
    method: 'POST',
    body: { reason: reason ?? 'restricted' },
  });
}

// ── Skills registry (mirrors Tools) ─────────────────────────────────────────────────────────
export async function listSkills(signal?: AbortSignal): Promise<SkillView[]> {
  const r = await api<{ data?: SkillView[] } | SkillView[]>('skills', '/v1/skills', { signal });
  return Array.isArray(r) ? r : (r.data ?? []);
}

export function getSkill(name: string, version?: string, signal?: AbortSignal): Promise<SkillView> {
  return api<SkillView>('skills', `/v1/skills/${encodeURIComponent(name)}`, {
    query: version ? { version } : undefined,
    signal,
  });
}

export function registerSkill(manifest: Record<string, unknown>): Promise<Record<string, unknown>> {
  return api('skills', '/v1/skills', { method: 'POST', body: manifest });
}

export function getSkillAccess(
  skillName: string,
  params: { agent_id?: string; capability?: string } = {},
  signal?: AbortSignal,
): Promise<AccessResolution> {
  return api<AccessResolution>('skills', `/v1/skills/${encodeURIComponent(skillName)}/access`, {
    query: { ...params },
    signal,
  });
}

export function setSkillAccess(
  skillName: string,
  body: { agent_id: string; access_mode: AccessMode; capability?: string },
): Promise<Record<string, unknown>> {
  return api('skills', `/v1/skills/${encodeURIComponent(skillName)}/access`, { method: 'PUT', body });
}

export function listRestrictedSkills(signal?: AbortSignal): Promise<{ data: Array<Record<string, unknown>> }> {
  return api('skills', '/v1/restricted-skills', { signal });
}

export function markSkillRestricted(skillName: string, reason?: string): Promise<Record<string, unknown>> {
  return api('skills', `/v1/restricted-skills/${encodeURIComponent(skillName)}`, {
    method: 'POST',
    body: { reason: reason ?? 'restricted' },
  });
}

// ── Guardrails: custom rules (tenant-authored) ───────────────────────────────────────────────
export async function listCustomRules(signal?: AbortSignal): Promise<CustomRule[]> {
  const r = await api<{ rules?: CustomRule[]; data?: CustomRule[] } | CustomRule[]>('guardrails', '/v1/rules', {
    signal,
  });
  return Array.isArray(r) ? r : (r.rules ?? r.data ?? []);
}

/** Single-rule reads/writes return a `{ rule: {...} }` envelope; unwrap to the bare record. */
function unwrapRule(r: { rule?: CustomRule } | CustomRule): CustomRule {
  return (r as { rule?: CustomRule }).rule ?? (r as CustomRule);
}

export async function getCustomRule(ruleId: string, signal?: AbortSignal): Promise<CustomRule> {
  return unwrapRule(await api<{ rule?: CustomRule } | CustomRule>('guardrails', `/v1/rules/${ruleId}`, { signal }));
}

export async function createCustomRule(body: CustomRuleInput): Promise<CustomRule> {
  return unwrapRule(
    await api<{ rule?: CustomRule } | CustomRule>('guardrails', '/v1/rules', { method: 'POST', body }),
  );
}

export async function updateCustomRule(ruleId: string, body: CustomRuleInput): Promise<CustomRule> {
  return unwrapRule(
    await api<{ rule?: CustomRule } | CustomRule>('guardrails', `/v1/rules/${ruleId}`, { method: 'PUT', body }),
  );
}

export function deleteCustomRule(ruleId: string): Promise<void> {
  return api<void>('guardrails', `/v1/rules/${ruleId}`, { method: 'DELETE' });
}

// ── Guardrails: check playground + policy simulation + assignment + redaction rotate ─────────────
/** Test text against the live effective policy (input direction). Always 200, even on block. */
export function checkInput(body: {
  text: string;
  input_text?: string;
  untrusted_spans?: string[];
}): Promise<CheckResult> {
  return api<CheckResult>('guardrails', '/v1/check/input', { method: 'POST', body });
}

/** Test text against the live effective policy (output direction). `input_text` = the original prompt. */
export function checkOutput(body: {
  text: string;
  input_text?: string;
  grounding?: string[];
}): Promise<CheckResult> {
  return api<CheckResult>('guardrails', '/v1/check/output', { method: 'POST', body });
}

/** Simulate a STORED policy against sample text — decision + per-rule trace, nothing persisted. */
export function simulateStoredPolicy(
  policyId: string,
  body: { text: string; input_text?: string; direction?: 'input' | 'output' },
): Promise<SimulationResult> {
  return api<SimulationResult>('guardrails', `/v1/policies/${policyId}/simulate`, { method: 'POST', body });
}

/** Simulate an INLINE DRAFT policy (unsaved rules) against sample text. */
export function simulateDraftPolicy(body: {
  text: string;
  input_text?: string;
  direction?: 'input' | 'output';
  rules: Array<{ rule_id: string; enabled: boolean; action_override?: string | null }>;
  fail_mode_override?: string | null;
  stream_mode?: string;
}): Promise<SimulationResult> {
  return api<SimulationResult>('guardrails', '/v1/policies/simulate', { method: 'POST', body });
}

/** Repoint an agent at a policy (atomic assignment). */
export function assignPolicy(policyId: string, agentId: string): Promise<Record<string, unknown>> {
  return api('guardrails', `/v1/policies/${policyId}/assign`, { method: 'POST', body: { agent_id: agentId } });
}

/** Rotate the tenant's redaction HMAC key (tenant:admin); the old key stays valid during grace. */
export function rotateRedactionKey(): Promise<Record<string, unknown>> {
  return api('guardrails', '/v1/redaction-keys/rotate', { method: 'POST', body: {} });
}

// ── LLMs: BYOK connection rotate ─────────────────────────────────────────────────────────────
/**
 * Rotate a BYOK provider key: register a new active secret and put the old one into a grace
 * window (both valid during grace). The raw secret is sent once and never returned.
 */
export function rotateLlmConnection(
  keyId: string,
  body: { secret: string; priority?: number },
): Promise<Record<string, unknown>> {
  return api('llms', `/v1/keys/${keyId}/rotate`, { method: 'POST', body });
}

// ── LLMs: chat completion (playground) ───────────────────────────────────────────────────────
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ChatCompletionResult {
  id?: string;
  model?: string;
  choices?: Array<{
    index?: number;
    message?: { role?: string; content?: string };
    finish_reason?: string;
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

/**
 * Run a chat completion through the gateway to test a model/alias/BYOK/rule end-to-end.
 * Forced non-streaming so it flows through the buffered BFF proxy; usage is returned inline.
 */
export function chatCompletion(body: {
  model: string;
  messages: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
}): Promise<ChatCompletionResult> {
  return api<ChatCompletionResult>('llms', '/v1/chat/completions', {
    method: 'POST',
    body: { ...body, stream: false },
  });
}

// ── LLMs: embeddings / classify / rerank testers ─────────────────────────────────────────────
export interface EmbeddingResult {
  data?: Array<{ embedding: number[]; index?: number }>;
  model?: string;
  usage?: { prompt_tokens?: number; total_tokens?: number; [k: string]: unknown };
  [k: string]: unknown;
}

/** Embed one or more strings — returns the vector(s) + token usage. */
export function createEmbeddings(body: { input: string | string[]; model?: string }): Promise<EmbeddingResult> {
  return api<EmbeddingResult>('llms', '/v1/embeddings', { method: 'POST', body });
}

export interface ClassifyResult {
  verdict?: string;
  categories?: Record<string, number> | Array<{ category: string; score: number }>;
  model?: string;
  [k: string]: unknown;
}

/** Safety-classify a string (verdict + per-category scores). Default alias 'safety-default'. */
export function classifyText(body: { input: string; model?: string }): Promise<ClassifyResult> {
  return api<ClassifyResult>('llms', '/v1/classify', { method: 'POST', body });
}

export interface RerankResult {
  results?: Array<{ index: number; relevance_score?: number; score?: number; [k: string]: unknown }>;
  model?: string;
  usage?: Record<string, unknown>;
  [k: string]: unknown;
}

/** Rerank candidate documents against a query (cross-encoder relevance). Default alias 'rerank-default'. */
export function rerankDocuments(body: {
  query: string;
  documents: Array<{ text: string }>;
  model?: string;
  top_n?: number;
}): Promise<RerankResult> {
  return api<RerankResult>('llms', '/v1/rerank', { method: 'POST', body });
}

// ── Auth: credential control (rotate key, edit/deactivate agent, revoke tokens) ──────────────────
/** Rotate an agent's API key: mints a new secret (shown once) + a grace window on the old key. */
export function rotateAgentKey(
  agentId: string,
  keyId: string,
  body: { scopes?: string[]; name?: string; expires_in_days?: number } = {},
): Promise<RotateKeyResponse> {
  return api<RotateKeyResponse>('auth', `/v1/agents/${agentId}/keys/${keyId}/rotate`, { method: 'POST', body });
}

export function updateAgent(
  agentId: string,
  body: { allowed_scopes?: string[]; capabilities?: unknown; metadata?: unknown },
): Promise<Agent> {
  return api<Agent>('auth', `/v1/agents/${agentId}`, { method: 'PATCH', body });
}

/** Deactivate an agent — cascade-revokes its keys + tokens. */
export function deactivateAgent(agentId: string): Promise<DeactivateAgentResult> {
  return api<DeactivateAgentResult>('auth', `/v1/agents/${agentId}`, { method: 'DELETE' });
}

/** Kill-switch a single token by its jti (immediate revocation via the shared Valkey mirror). */
export function revokeToken(jti: string, reason?: string): Promise<void> {
  return api<void>('auth', '/v1/tokens/revoke', { method: 'POST', body: { jti, reason } });
}

/** Revoke ALL of an agent's outstanding tokens (bumps its revocation epoch). */
export function revokeAllTokens(agentId: string, reason?: string): Promise<Record<string, unknown>> {
  return api('auth', `/v1/agents/${agentId}/revoke-all-tokens`, { method: 'POST', body: { reason } });
}

// ── Auth admin: webhooks (tenant:admin) ──────────────────────────────────────────────────────
// The auth service uses Contract-21 wire names (`sub_id`, `signing_secret`, `last_status_code`);
// normalize them to the frontend's `id`/`secret`/`response_status` so the UI has one clean shape.
function normWebhook(w: Webhook): Webhook {
  const raw = w as Record<string, unknown>;
  return {
    ...w,
    id: (w.id ?? (raw.sub_id as string | undefined)) as string,
    secret: (w.secret ?? (raw.signing_secret as string | undefined)) ?? undefined,
  };
}

function normDelivery(d: WebhookDelivery): WebhookDelivery {
  const raw = d as Record<string, unknown>;
  return {
    ...d,
    id: (d.id ?? d.delivery_id) as string | undefined,
    response_status: (d.response_status ?? (raw.last_status_code as number | null | undefined)) ?? null,
  };
}

export async function listWebhooks(signal?: AbortSignal): Promise<Webhook[]> {
  const r = await api<{ subscriptions?: Webhook[]; data?: Webhook[] } | Webhook[]>('auth', '/v1/webhooks', { signal });
  const arr = Array.isArray(r) ? r : (r.subscriptions ?? r.data ?? []);
  return arr.map(normWebhook);
}

/** Create a webhook subscription. `event_types: ['*']` = all events. Returns the signing secret ONCE. */
export async function createWebhook(body: { url: string; event_types: string[] }): Promise<Webhook> {
  return normWebhook(await api<Webhook>('auth', '/v1/webhooks', { method: 'POST', body }));
}

export function deleteWebhook(id: string): Promise<void> {
  return api<void>('auth', `/v1/webhooks/${id}`, { method: 'DELETE' });
}

/** Rotate a webhook's signing secret; the new secret is returned ONCE. */
export async function rotateWebhookSecret(id: string): Promise<Webhook> {
  return normWebhook(await api<Webhook>('auth', `/v1/webhooks/${id}/rotate-secret`, { method: 'POST', body: {} }));
}

/** Resume a paused/disabled webhook (re-enable delivery). */
export async function resumeWebhook(id: string): Promise<Webhook> {
  return normWebhook(await api<Webhook>('auth', `/v1/webhooks/${id}/resume`, { method: 'POST', body: {} }));
}

/**
 * Replay deliveries: a specific one when `deliveryId` is given, else ALL recent failed deliveries
 * (the backend now treats an absent delivery_id as "replay recent failures" and returns `{replayed}`).
 */
export function replayWebhook(id: string, deliveryId?: string): Promise<Record<string, unknown>> {
  const body = deliveryId ? { delivery_id: deliveryId } : {};
  return api('auth', `/v1/webhooks/${id}/replay`, { method: 'POST', body });
}

export async function listWebhookDeliveries(id: string, signal?: AbortSignal): Promise<WebhookDelivery[]> {
  const r = await api<{ deliveries?: WebhookDelivery[]; data?: WebhookDelivery[] } | WebhookDelivery[]>(
    'auth',
    `/v1/webhooks/${id}/deliveries`,
    { signal },
  );
  const arr = Array.isArray(r) ? r : (r.deliveries ?? r.data ?? []);
  return arr.map(normDelivery);
}

// ── Auth admin: tenant settings (self) ─────────────────────────────────────────────────────────
export function getMyTenant(signal?: AbortSignal): Promise<TenantView> {
  return api<TenantView>('auth', '/v1/tenants/me', { signal });
}

/** Update the caller's own tenant (tenant:admin). Currently editable: display name + metadata. */
export function updateMyTenant(body: { name?: string; source_metadata?: Record<string, unknown> }): Promise<TenantView> {
  return api<TenantView>('auth', '/v1/tenants/me', { method: 'PATCH', body });
}

// ── Auth admin: audit export ─────────────────────────────────────────────────────────────────
/**
 * Build a cookie-authenticated download URL for the audit-log export. Use it as an
 * `<a href download>` (same-origin GET rides the session cookie) — the response is a file
 * (CSV/NDJSON), so it must NOT go through the JSON `api()` path.
 */
export function auditExportUrl(params: { from?: string; to?: string; format?: string } = {}): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) qs.set(k, String(v));
  const s = qs.toString();
  return streamUrl('auth', `/v1/audit-log/export${s ? `?${s}` : ''}`);
}

// ── Auth admin: platform admin (scope-gated on platform:admin) ───────────────────────────────────
export async function listSigningKeys(signal?: AbortSignal): Promise<SigningKeyView[]> {
  const r = await api<{ keys?: SigningKeyView[]; data?: SigningKeyView[] } | SigningKeyView[]>(
    'auth',
    '/v1/admin/signing-keys',
    { signal },
  );
  return Array.isArray(r) ? r : (r.keys ?? r.data ?? []);
}

/** Rotate the signing key (graceful — promotes the staged next key). */
export function rotateSigningKey(): Promise<Record<string, unknown>> {
  return api('auth', '/v1/admin/signing-keys/rotate', { method: 'POST', body: {} });
}

/**
 * Emergency signing-key rotation (immediate; invalidates tokens signed by the old key). Gated by an
 * out-of-band emergency token, sent as `X-Emergency-Token` (the BFF forwards it) — the operator must
 * supply the value provisioned in the auth service's emergency-rotate token file.
 */
export function emergencyRotateSigningKey(emergencyToken: string): Promise<Record<string, unknown>> {
  return api('auth', '/v1/admin/signing-keys/emergency-rotate', {
    method: 'POST',
    body: {},
    headers: emergencyToken ? { 'X-Emergency-Token': emergencyToken } : undefined,
  });
}

export async function listServiceClients(signal?: AbortSignal): Promise<ServiceClientView[]> {
  const r = await api<{ data?: ServiceClientView[]; clients?: ServiceClientView[] } | ServiceClientView[]>(
    'auth',
    '/v1/admin/service-clients',
    { signal },
  );
  return Array.isArray(r) ? r : (r.data ?? r.clients ?? []);
}

export function createServiceClient(body: {
  name: string;
  service_name?: string;
  scopes: string[];
}): Promise<ServiceClientView> {
  return api<ServiceClientView>('auth', '/v1/admin/service-clients', { method: 'POST', body });
}

export function deleteServiceClient(id: string): Promise<void> {
  return api<void>('auth', `/v1/admin/service-clients/${id}`, { method: 'DELETE' });
}

/** Rotate a service client's secret; the new secret is returned ONCE. */
export function rotateServiceClientSecret(id: string): Promise<ServiceClientView> {
  return api<ServiceClientView>('auth', `/v1/admin/service-clients/${id}/rotate-secret`, { method: 'POST', body: {} });
}
