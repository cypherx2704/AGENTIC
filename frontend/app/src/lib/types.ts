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
  // Per-agent tool-loop toggle (xAgent migration 0007). true (default) = "multiple
  // request": the full LLM<->tool loop runs (multiple LLM calls). false = "per request":
  // the tool loop is skipped so the task makes a single LLM call — for rate-limited /
  // free-tier models. Optional in the response for back-compat with a pre-0007 gateway.
  tool_loop_enabled?: boolean;
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
  // See AgentRuntime.tool_loop_enabled. Optional on write: omitting it lets the gateway
  // default to true (current multi-call behaviour). Set false for single-call "per request".
  tool_loop_enabled?: boolean;
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
  description?: string | null;
  status: string;
  embedding_model?: string;
  embedding_model_alias?: string;
  embedding_model_resolved?: string;
  embedding_dim?: number;
  chunking_strategy?: string;
  chunk_size?: number;
  chunk_overlap?: number;
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

/** Full KB record (RAG `KbResponse`) — returned by create / get / (list rows). */
export interface KbDetail {
  kb_id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  chunking_strategy: string;
  chunk_size: number;
  chunk_overlap: number;
  embedding_model_alias: string;
  embedding_model_resolved: string;
  embedding_dim: number;
  status: string;
  created_at: string;
  updated_at: string;
}

/** KB rollup counts (RAG `GET /v1/kbs/{id}/status`). */
export interface KbStatus {
  kb_id: string;
  document_count: number;
  chunk_count: number;
  pending_docs: number;
  failed_docs: number;
  last_updated_at: string | null;
}

/** A document in a KB (RAG `DocumentResponse`). status ∈ pending|processing|completed|failed. */
export interface RagDocument {
  doc_id: string;
  kb_id: string;
  name: string;
  source_type: string;
  source_uri: string | null;
  status: string;
  attempts: number;
  error_msg: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface RagDocumentListResponse {
  documents: RagDocument[];
  next_offset: number | null;
}

/** Presigned-upload grant (RAG `UploadUrlResponse`). */
export interface RagUploadUrl {
  upload_url: string;
  doc_id: string;
  fields: Record<string, string>;
  expires_in: number;
}

/** A single retrieval hit (RAG `QueryHit`). */
export interface RagQueryHit {
  chunk_id: string;
  doc_id: string;
  content: string;
  score: number;
  metadata: Record<string, unknown>;
  source: { name: string; uri?: string | null };
}

export interface RagQueryResponse {
  results: RagQueryHit[];
  query_id: string;
  duration_ms: number;
}

export type RagSearchMode = 'dense' | 'hybrid' | 'sparse';
export type RagPrincipalType = 'agent' | 'api_key' | 'user' | 'role' | 'tenant';
export type RagPermission = 'read' | 'query' | 'ingest' | 'write' | 'admin';

/** A KB ACL grant (RAG `AclRow` / `AclResponse`). */
export interface RagAcl {
  principal_type: RagPrincipalType;
  principal_id: string;
  permissions: RagPermission[];
  expires_at?: string | null;
  kb_id?: string;
  created_by?: string;
  created_at?: string;
}

// ── Memory service (principal-scoped agent memory) ───────────────────────────────────
/** Cross-principal visibility of a memory. `principal_only` (default) is private. */
export type MemoryVisibility = 'principal_only' | 'tenant_shared';

/** A stored memory (Memory `MemoryRecord`). Search adds `similarity`/`composite_score`. */
export interface MemoryRecord {
  id: string;
  principal_type: string;
  principal_id: string;
  scope: MemoryVisibility;
  type: string;
  tags: string[];
  content: string;
  metadata: Record<string, unknown>;
  session_id: string | null;
  score: number;
  created_at: string;
  last_accessed_at: string;
  expires_at?: string | null;
  importance_score?: number | null;
  last_retrieved_at?: string | null;
  valid_until?: string | null;
  superseded_by_id?: string | null;
  access_count?: number | null;
  session_scope_id?: string | null;
  agent_scope_id?: string | null;
  similarity?: number | null;
  composite_score?: number | null;
  deduped?: boolean | null;
}

export interface MemorySearchResponse {
  results: MemoryRecord[];
  count: number;
}

/** A memory session (Memory `SessionRecord`). */
export interface MemorySession {
  session_id: string;
  principal_type: string;
  principal_id: string;
  title: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface GdprWipeResult {
  principal_type: string;
  principal_id: string;
  deleted_count: number;
  wipe_log_id: string;
}

// ── Tools & Skills registries (MCP servers) ──────────────────────────────────────────
/** Per-agent access mode for a tool/skill capability. */
export type AccessMode = 'none' | 'ask' | 'automated';

/**
 * A resolved registry entry (tool or skill) from discovery — the backend `build_*_view`
 * shape. Typed permissively (manifest/health/capabilities vary) with the common fields the
 * catalog + detail views render; the index signature admits anything else the view carries.
 */
export interface RegistryEntry {
  tool_id?: string;
  skill_id?: string;
  name: string;
  owner?: string; // 'platform' | 'tenant'
  is_platform?: boolean;
  version?: string;
  resolved_version?: string;
  latest_version?: string;
  description?: string | null;
  manifest?: Record<string, unknown> | null;
  capabilities?: Array<Record<string, unknown>>;
  required_scopes?: string[];
  invoke_url?: string;
  health?: Record<string, unknown> | string | null;
  restricted?: boolean;
  [k: string]: unknown;
}
export type ToolView = RegistryEntry;
export type SkillView = RegistryEntry;

/** Effective access resolution for an agent + tool/skill (GET .../access). */
export interface AccessResolution {
  tool?: string;
  skill?: string;
  agent_id: string;
  capability?: string | null;
  access_mode: AccessMode;
  restricted: boolean;
}

// ── Tool Builder (flow-tool-bridge) ────────────────────────────────────────────────────
export type FlowToolParamType = 'string' | 'integer' | 'number' | 'boolean';

/** One input/output parameter defined in the Publish dialog form (-> JSON Schema). */
export interface FlowToolParam {
  name: string;
  type: FlowToolParamType;
  required?: boolean;
  description?: string;
}

/** A published flow-tool as returned by the bridge list/detail (no secrets/internal hosts). */
export interface FlowTool {
  slug: string;
  server_name: string;
  tool_name: string;
  display_name: string;
  description: string;
  version: string;
  access_mode: AccessMode;
  status: 'active' | 'retired';
  node_red_flow_id?: string | null;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown> | null;
  updated_at?: string | null;
}

/** Body for POST /v1/flow-tools (publish). */
export interface PublishFlowToolRequest {
  node_red_flow_id: string;
  tool: {
    title: string;
    snake_name?: string;
    description: string;
    access_mode?: AccessMode;
    input_params?: FlowToolParam[];
    output_params?: FlowToolParam[];
  };
}

export interface PublishFlowToolResult {
  slug: string;
  server_name: string;
  tool_name: string;
  version: string;
  invoke_url: string;
  access_mode: AccessMode;
  is_update: boolean;
}

/** Response of POST /v1/editor-sessions. */
export interface EditorSession {
  ready: boolean;
  runtime_status: string;
  editor_url: string;
  expires_at: string;
}

/** A Node-RED flow tab, for the Publish dialog's workflow picker. */
export interface NoderedFlow {
  id: string;
  label: string;
}

// ── Guardrails: custom rules + check + simulation ────────────────────────────────────
export type RuleDirection = 'input' | 'output' | 'both';
export type RuleSeverity = 'info' | 'low' | 'medium' | 'high' | 'critical';
export type RuleAction = 'allow' | 'warn' | 'redact' | 'block';
export type RuleFailMode = 'closed' | 'open';
export type CustomRuleType = 'regex' | 'classifier-threshold';

/** A tenant-authored custom rule (guardrails `CustomRule`; stable `id` = root_rule_id). */
export interface CustomRule {
  id: string;
  rule_id: string;
  tenant_id: string;
  version: number;
  name: string;
  type: string;
  direction: string;
  category: string;
  severity: string;
  default_action: string;
  default_fail_mode: string;
  timeout_ms: number;
  status: string;
  pattern?: string | null;
  classifier_category?: string | null;
  threshold?: number | null;
}

/** Body of create/update custom rule (guardrails `CustomRuleCreate`). */
export interface CustomRuleInput {
  name: string;
  type: CustomRuleType;
  direction?: RuleDirection;
  category: string;
  severity?: RuleSeverity;
  default_action?: RuleAction;
  default_fail_mode?: RuleFailMode;
  timeout_ms?: number;
  pattern?: string | null;
  classifier_category?: string | null;
  threshold?: number | null;
}

export type CheckDecision = 'allow' | 'warn' | 'redact' | 'block';

/** A violation surfaced by a check (matched text is always a redaction token / truncation). */
export interface CheckViolation {
  rule_id?: string;
  rule_name?: string;
  category?: string;
  severity?: string;
  action?: string;
  matched?: string;
  [k: string]: unknown;
}

/** Result of `POST /v1/check/{input,output}` — the guardrails test playground. */
export interface CheckResult {
  decision: CheckDecision;
  processed_text?: string | null;
  violations: CheckViolation[];
  check_id?: string;
  duration_ms?: number;
  trace_id?: string;
  confidence?: number;
  metadata?: Record<string, unknown> | null;
}

/** Result of a policy simulation (decision + per-rule evaluation trace; nothing persisted). */
export interface SimulationResult {
  decision: CheckDecision;
  evaluation_trace?: Array<Record<string, unknown>>;
  processed_text?: string | null;
  violations?: CheckViolation[];
  [k: string]: unknown;
}

// ── Auth: credential control (key rotate / agent deactivate cascade) ──────────────────
/** Response of `POST /v1/agents/{id}/keys/{keyId}/rotate` — the new secret is shown ONCE. */
export interface RotateKeyResponse {
  key_id: string;
  api_key: string;
  key_prefix: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
  previous_key_id: string;
  previous_key_expires_at: string;
}

/** Result of deactivating an agent (cascade revokes its keys + tokens). */
export interface DeactivateAgentResult {
  agent: Agent;
  keys_revoked: number;
  tokens_revoked: number;
}

// ── Auth admin: webhooks ─────────────────────────────────────────────────────────────
/** A webhook subscription (auth `/v1/webhooks`). `secret` is present ONLY on create/rotate. */
export interface Webhook {
  id: string;
  url: string;
  event_types: string[];
  status?: string; // active | paused | disabled
  secret?: string; // shown once on create / rotate-secret
  created_at?: string;
  updated_at?: string;
  last_delivery_at?: string | null;
  failure_count?: number;
  [k: string]: unknown;
}

/** A single webhook delivery attempt (auth `/v1/webhooks/{id}/deliveries`). */
export interface WebhookDelivery {
  id?: string;
  delivery_id?: string;
  event_type?: string;
  status?: string; // delivered | failed | pending
  response_status?: number | null;
  attempts?: number;
  created_at?: string;
  delivered_at?: string | null;
  [k: string]: unknown;
}

// ── Auth admin: tenant settings (self, via /v1/tenants/me) ───────────────────────────
/** The caller's tenant (auth `GET/PATCH /v1/tenants/me`). Render `name` — never `tenant_id`. */
export interface TenantView {
  tenant_id: string;
  name: string;
  status: string;
  plan: string;
  source?: string;
  source_metadata?: Record<string, unknown>;
  region?: string | null;
  created_at?: string;
  updated_at?: string;
  suspended_at?: string | null;
  pending_deletion_at?: string | null;
  deleted_at?: string | null;
  [k: string]: unknown;
}

// ── Auth admin: platform admin (scope-gated on platform:admin) ────────────────────────
/** A signing key (auth `/v1/admin/signing-keys`). */
export interface SigningKeyView {
  kid?: string;
  key_id?: string;
  status?: string; // active | next | retiring | retired
  algorithm?: string;
  created_at?: string;
  not_after?: string | null;
  [k: string]: unknown;
}

/** A service client (auth `/v1/admin/service-clients`). `secret` present only on create/rotate. */
export interface ServiceClientView {
  id?: string;
  client_id?: string;
  name?: string;
  service_name?: string;
  scopes?: string[];
  status?: string;
  secret?: string; // shown once
  created_at?: string;
  [k: string]: unknown;
}

// ── Platform health ──────────────────────────────────────────────────────────────────
export interface HealthProbe {
  service: string;
  livez: number | null;
  readyz: number | null;
}
