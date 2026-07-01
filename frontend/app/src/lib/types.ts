/**
 * Shared API types — mirror the CypherX service contracts so the BFF responses are typed
 * end-to-end. Field names match the wire JSON (snake_case) the platform services emit.
 */

// ── Contract 2: the canonical error envelope ─────────────────────────────────────────
export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    request_id?: string;
    trace_id?: string;
    timestamp?: string;
    details?: Record<string, unknown> | null;
  };
}

// ── Session (BFF /bff/me) ────────────────────────────────────────────────────────────
export interface Session {
  authenticated: boolean;
  tenant_id: string | null;
  scopes: string[];
  csrf_token: string | null;
}

// ── Auth: agents + keys ──────────────────────────────────────────────────────────────
export interface Agent {
  agent_id: string;
  tenant_id: string;
  name: string;
  version: string;
  status: string;
  allowed_scopes: string[];
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ApiKeyListItem {
  key_id: string;
  agent_id: string;
  key_prefix: string;
  name: string | null;
  scopes: string[];
  status: string;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface CreateKeyResponse {
  key_id: string;
  api_key: string; // the raw secret — shown exactly ONCE
  key_prefix: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
}

// ── xAgent: runtime config (Component 1) ─────────────────────────────────────────────
export type MemoryScope = 'none' | 'agent' | 'user' | 'tenant' | 'session';
export type AgentRuntimeStatus = 'active' | 'inactive' | 'pending_config';

export interface AgentRuntime {
  agent_id: string;
  tenant_id: string;
  name: string;
  runtime_version: string;
  status: AgentRuntimeStatus;
  llm_model: string;
  system_prompt: string;
  max_tokens: number;
  temperature: number;
  memory_scope: MemoryScope;
  guardrail_policy_id: string | null;
  allowed_tools: string[];
  allowed_skills: string[];
  allowed_kb_ids: string[];
  rag_top_k_per_kb: number;
  rag_min_score: number;
  token_budget_per_task: number;
  capabilities: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
}

export interface AgentRuntimeRegistration {
  name: string;
  status: AgentRuntimeStatus;
  llm_model: string;
  system_prompt: string;
  max_tokens: number;
  temperature: number;
  memory_scope: MemoryScope;
  guardrail_policy_id?: string | null;
  allowed_tools: string[];
  allowed_skills: string[];
  allowed_kb_ids: string[];
  rag_top_k_per_kb: number;
  rag_min_score: number;
  token_budget_per_task: number;
}

// ── LLMs: models / usage / cost ──────────────────────────────────────────────────────
export interface LlmModel {
  id: string;
  provider: string;
  aliases: string[];
  capabilities: {
    max_tokens_cap?: number;
    context_window?: number;
    supports_vision?: boolean;
    supports_tools?: boolean;
    supports_streaming?: boolean;
    embedding_dim?: number | null;
  };
}

export interface UsageRow {
  // group_by keys (any of: date, model, agent_id, ...) appear as columns alongside totals.
  [key: string]: string | number | null | undefined;
  prompt_tokens?: number;
  completion_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  total_tokens?: number;
  request_count?: number;
}

export interface CostRow {
  [key: string]: string | number | null | undefined;
  cost_usd?: number;
  total_tokens?: number;
  request_count?: number;
}

export interface GroupedResult<T> {
  group_by: string[];
  from: string | null;
  to: string | null;
  data: T[];
}

// ── xAgent: tasks (Contract 3) ───────────────────────────────────────────────────────
export type TaskStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timeout';

export interface TaskStep {
  step: string;
  status: string;
  duration_ms?: number | null;
  tokens?: number | null;
}

export interface TaskResponse {
  task_id: string;
  status: TaskStatus;
  trace_id?: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  tokens_used: number;
  cost_usd: number;
  output?: { message?: string; [k: string]: unknown } | null;
  task_steps: TaskStep[];
  error?: ApiErrorEnvelope['error'] | null;
  metadata?: Record<string, unknown> | null;
}

export interface TaskListItem {
  task_id: string;
  agent_id: string;
  status: TaskStatus;
  trace_id?: string;
  error_code?: string | null;
  tokens_used: number | null;
  cost_usd: number | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface TaskListResponse {
  tasks: TaskListItem[];
  next_cursor: string | null;
}

// ── Guardrails: policies + violations ────────────────────────────────────────────────
export interface PolicyRule {
  rule_id: string;
  enabled: boolean;
  action_override?: string | null;
}

export interface Policy {
  policy_id: string;
  name: string;
  tenant_id: string | null;
  is_default?: boolean;
  status: string;
  rules: PolicyRule[];
  version?: number;
  stream_mode?: string;
  fail_mode_override?: string | null;
}

export interface PolicyListResponse {
  policies: Policy[];
}

export interface Violation {
  id: string;
  check_id: string | null;
  request_id: string | null;
  agent_id: string | null;
  task_id: string | null;
  trace_id: string | null;
  policy_id: string | null;
  direction: string;
  decision: string;
  rule_id: string | null;
  rule_name: string | null;
  severity: string | null;
  category: string | null;
  matched: string | null;
  created_at: string;
}

export interface ViolationListResponse {
  violations: Violation[];
  next_cursor: string | null;
  has_more: boolean;
}

// ── Audit log ────────────────────────────────────────────────────────────────────────
export interface AuditRow {
  id: number;
  event_type: string;
  agent_id: string | null;
  tenant_id: string;
  action: string | null;
  resource: string | null;
  decision: string | null;
  policy_ids?: unknown;
  request_id: string | null;
  trace_id: string | null;
  ip_address: string | null;
  created_at: string;
  row_hash: string;
  prev_row_hash: string;
}

export interface AuditListResponse {
  items: AuditRow[];
  next_cursor: string | null;
}

export interface AuditVerifyResult {
  ok: boolean;
  rows_verified?: number;
  from_hash?: string;
  to_hash?: string;
  broken_at_row_id?: number;
  expected_prev_hash?: string;
  actual_prev_hash?: string;
}

// ── RAG: knowledge bases ─────────────────────────────────────────────────────────────
export interface KnowledgeBase {
  kb_id: string;
  tenant_id?: string;
  name: string;
  status: string;
  embedding_model?: string;
  document_count?: number;
  chunk_count?: number;
  created_at?: string;
  updated_at?: string;
  [k: string]: unknown;
}

export interface KbListResponse {
  data?: KnowledgeBase[];
  knowledge_bases?: KnowledgeBase[];
  [k: string]: unknown;
}

// ── Platform health ──────────────────────────────────────────────────────────────────
export interface HealthProbe {
  service: string;
  livez: number | null;
  readyz: number | null;
}
