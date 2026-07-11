'use client';

import type { ReactNode } from 'react';
import { useMemo, useState } from 'react';
import { Pipeline, type PipelineStage, type StageState } from '@/components/Pipeline';
import { Badge, Button, Card, CardBody, CardHeader, ErrorBanner, Input, Select, Switch, Textarea, useToast } from '@/components/ui';
import { putRuntime } from '@/lib/services';
import type {
  AgentRuntime,
  AgentRuntimeRegistration,
  AgentRuntimeStatus,
  KnowledgeBase,
  LlmModel,
  MemoryScope,
  Policy,
} from '@/lib/types';
import { cn, shortId } from '@/lib/utils';

// The full memory_scope enum — including "session" — matching the xAgent runtime model.
const MEMORY_SCOPES: MemoryScope[] = ['none', 'agent', 'user', 'tenant', 'session'];
const STATUSES: AgentRuntimeStatus[] = ['pending_config', 'active', 'inactive'];
const STATUS_LABEL: Record<AgentRuntimeStatus, string> = {
  pending_config: 'Pending Config',
  active: 'Active',
  inactive: 'Inactive',
};

interface FormState {
  name: string;
  status: AgentRuntimeStatus;
  llm_model: string;
  system_prompt: string;
  max_tokens: number;
  temperature: number;
  memory_scope: MemoryScope;
  guardrail_policy_id: string;
  allowed_tools: string;
  // true = "multiple request" (full tool loop, multiple LLM calls); false = "per request"
  // (skip the tool loop -> a single LLM call, for rate-limited / free-tier models).
  tool_loop_enabled: boolean;
  allowed_skills: string;
  allowed_kb_ids: string[];
  rag_top_k_per_kb: number;
  rag_min_score: number;
  token_budget_per_task: number;
}

function toForm(rt: AgentRuntime | null, fallbackName: string): FormState {
  return {
    name: rt?.name ?? fallbackName,
    status: rt?.status ?? 'pending_config',
    llm_model: rt?.llm_model ?? 'smart',
    system_prompt: rt?.system_prompt ?? 'You are a helpful assistant. Answer concisely.',
    max_tokens: rt?.max_tokens ?? 2048,
    temperature: rt?.temperature ?? 0.7,
    memory_scope: rt?.memory_scope ?? 'agent',
    guardrail_policy_id: rt?.guardrail_policy_id ?? '',
    allowed_tools: (rt?.allowed_tools ?? []).join(', '),
    // Default true (multiple request) — preserves the current behaviour when the field is
    // absent (a pre-0007 runtime row / gateway that never returned it).
    tool_loop_enabled: rt?.tool_loop_enabled ?? true,
    allowed_skills: (rt?.allowed_skills ?? []).join(', '),
    allowed_kb_ids: rt?.allowed_kb_ids ?? [],
    rag_top_k_per_kb: rt?.rag_top_k_per_kb ?? 5,
    rag_min_score: rt?.rag_min_score ?? 0.7,
    token_budget_per_task: rt?.token_budget_per_task ?? 10000,
  };
}

function csv(value: string): string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function toRegistration(f: FormState, status: AgentRuntimeStatus): AgentRuntimeRegistration {
  return {
    name: f.name.trim(),
    status,
    llm_model: f.llm_model,
    system_prompt: f.system_prompt,
    max_tokens: Number(f.max_tokens),
    temperature: Number(f.temperature),
    memory_scope: f.memory_scope,
    guardrail_policy_id: f.guardrail_policy_id.trim() || null,
    allowed_tools: csv(f.allowed_tools),
    tool_loop_enabled: f.tool_loop_enabled,
    allowed_skills: csv(f.allowed_skills),
    allowed_kb_ids: f.allowed_kb_ids,
    rag_top_k_per_kb: Number(f.rag_top_k_per_kb),
    rag_min_score: Number(f.rag_min_score),
    token_budget_per_task: Number(f.token_budget_per_task),
  };
}

/** A titled block within the config card. Dense, typographic, divided by thin rules. */
function Section({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return (
    <div className="border-t border-border px-4 py-4 first:border-t-0">
      <div className="mb-3">
        <h4 className="text-[11px] font-semibold uppercase tracking-wider text-faint">{title}</h4>
        {desc && <p className="mt-0.5 text-xs text-muted">{desc}</p>}
      </div>
      {children}
    </div>
  );
}

/** Derive the execution-pipeline spine from the current config — it updates live as you edit. */
function configStages(f: FormState, policies: Policy[]): PipelineStage[] {
  const policyName = f.guardrail_policy_id
    ? policies.find((p) => p.policy_id === f.guardrail_policy_id)?.name ?? shortId(f.guardrail_policy_id, 8)
    : 'Default';
  const toolCount = csv(f.allowed_tools).length;
  const on = (b: boolean): StageState => (b ? 'done' : 'idle');
  return [
    { label: 'Guard In', state: 'done', meta: policyName },
    { label: 'Prompt', state: 'done', meta: `${f.max_tokens} tok` },
    { label: 'LLM', state: 'done', meta: f.llm_model },
    { label: 'Guard Out', state: 'done', meta: policyName },
    { label: 'Tools', state: on(toolCount > 0 && f.tool_loop_enabled), meta: toolCount ? `${toolCount} tool${toolCount > 1 ? 's' : ''}` : 'Off' },
    { label: 'Memory', state: on(f.memory_scope !== 'none'), meta: f.memory_scope === 'none' ? 'Off' : f.memory_scope },
  ];
}

/**
 * The Agent Builder: create/edit an agent's xAgent runtime via PUT
 * /bff/api/xagent/v1/agents/{id}/runtime, then a two-step PUBLISH:
 *   step 1 — save the config (status stays as-is / pending_config on first create)
 *   step 2 — flip status to `active`
 * If step 2 fails (e.g. a transient downstream error), step 1's save is preserved and a
 * "Retry publish" button re-attempts ONLY step 2.
 */
export function AgentBuilder({
  agentId,
  fallbackName,
  initialRuntime,
  models,
  policies = [],
  kbs = [],
  onSaved,
}: {
  agentId: string;
  fallbackName: string;
  initialRuntime: AgentRuntime | null;
  models: LlmModel[];
  policies?: Policy[];
  kbs?: KnowledgeBase[];
  onSaved: (rt: AgentRuntime) => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<FormState>(() => toForm(initialRuntime, fallbackName));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [dirty, setDirty] = useState(false);
  // Two-step publish tracking: after a successful step-1 save we may still need step 2.
  const [publishStep2Pending, setPublishStep2Pending] = useState(false);

  function patch<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    setDirty(true);
  }
  function toggleKb(id: string) {
    setForm((f) => ({
      ...f,
      allowed_kb_ids: f.allowed_kb_ids.includes(id) ? f.allowed_kb_ids.filter((x) => x !== id) : [...f.allowed_kb_ids, id],
    }));
    setDirty(true);
  }

  const modelOptions = useMemo(() => {
    const ids = new Set<string>(['smart', 'fast']);
    for (const m of models) {
      ids.add(m.id);
      for (const a of m.aliases) ids.add(a);
    }
    // Keep the current value selectable even if the catalog doesn't list it.
    if (form.llm_model) ids.add(form.llm_model);
    return Array.from(ids).sort();
  }, [models, form.llm_model]);

  const policiesAvailable = policies.length > 0;
  const kbsAvailable = kbs.length > 0;
  const stages = configStages(form, policies);

  /** Save the runtime config only (no status flip beyond what's in the form). */
  async function save(statusOverride?: AgentRuntimeStatus) {
    setSaving(true);
    setError(null);
    const status = statusOverride ?? form.status;
    const rt = await putRuntime(agentId, toRegistration(form, status));
    setForm(toForm(rt, fallbackName));
    setDirty(false);
    onSaved(rt);
    setSaving(false);
    return rt;
  }

  async function onSaveDraft(e: React.FormEvent) {
    e.preventDefault();
    try {
      await save();
      toast.success('Runtime configuration saved.');
    } catch (err) {
      setError(err);
      setSaving(false);
    }
  }

  /** Two-step publish: step 1 save, step 2 activate. */
  async function onPublish() {
    setError(null);
    setSaving(true);
    // Step 1 — persist the latest config (keep current status; never regress to pending).
    try {
      await save(form.status === 'pending_config' ? 'pending_config' : form.status);
    } catch (err) {
      setError(err);
      setSaving(false);
      toast.error('Publish step 1 (save) failed.');
      return;
    }
    // Step 2 — activate.
    try {
      const activated = await save('active');
      setPublishStep2Pending(false);
      toast.success(`Agent published — status is now ${activated.status}.`);
    } catch (err) {
      // Step 1 succeeded; only step 2 (activate) failed. Offer a step-2-only retry.
      setError(err);
      setPublishStep2Pending(true);
      setSaving(false);
      toast.error('Publish step 2 (activate) failed — your config is saved. You can retry just the activation.');
    }
  }

  /** Retry ONLY step 2 (activate) after a step-1 success + step-2 failure. */
  async function onRetryPublish() {
    setError(null);
    setSaving(true);
    try {
      const activated = await save('active');
      setPublishStep2Pending(false);
      toast.success(`Activation succeeded — status is now ${activated.status}.`);
    } catch (err) {
      setError(err);
      setSaving(false);
      toast.error('Activation retry failed.');
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {/* execution-pipeline spine — reflects the config live */}
      <Card>
        <CardHeader
          title="Execution Pipeline"
          description="How this agent will run a task — each stage reflects the configuration below."
          actions={
            <Badge tone={form.status === 'active' ? 'success' : form.status === 'inactive' ? 'danger' : 'warning'}>
              {STATUS_LABEL[form.status]}
            </Badge>
          }
        />
        <CardBody>
          <Pipeline stages={stages} className="px-1 py-1" />
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Runtime Configuration"
          description="Everything this agent needs to run — saved to the xAgent runtime."
          actions={
            <div className="flex items-center gap-2.5">
              {dirty && (
                <span className="inline-flex items-center gap-1.5 text-xs font-medium text-warning">
                  <span className="h-1.5 w-1.5 rounded-full bg-warning" />
                  Unsaved Changes
                </span>
              )}
              <Button variant="secondary" size="md" onClick={onSaveDraft} loading={saving && !publishStep2Pending}>
                Save Config
              </Button>
              {publishStep2Pending ? (
                <Button size="md" onClick={onRetryPublish} loading={saving}>
                  Retry Publish
                </Button>
              ) : (
                <Button size="md" onClick={onPublish} loading={saving}>
                  Publish
                </Button>
              )}
            </div>
          }
        />

        <form onSubmit={onSaveDraft}>
          <Section title="Model & Generation" desc="The model, decoding, and per-task limits.">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Input label="Display Name" value={form.name} onChange={(e) => patch('name', e.target.value)} required />
              <Select label="Status" value={form.status} onChange={(e) => patch('status', e.target.value as AgentRuntimeStatus)}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {STATUS_LABEL[s]}
                  </option>
                ))}
              </Select>

              <Select
                label="LLM Model"
                value={form.llm_model}
                onChange={(e) => patch('llm_model', e.target.value)}
                hint="Resolvable models + aliases for this tenant (from the LLMs gateway)."
              >
                {modelOptions.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </Select>

              <div className="flex flex-col gap-1.5">
                <div className="flex items-baseline justify-between">
                  <label htmlFor="ab-temp" className="text-sm font-medium text-fg">
                    Temperature
                  </label>
                  <span className="font-mono text-xs tabular-nums text-muted">{form.temperature.toFixed(2)}</span>
                </div>
                <input
                  id="ab-temp"
                  type="range"
                  min={0}
                  max={2}
                  step={0.05}
                  value={form.temperature}
                  onChange={(e) => patch('temperature', Number(e.target.value))}
                  className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-brand"
                />
                <p className="text-xs text-muted">0 = deterministic · 2 = most creative.</p>
              </div>

              <Input
                label="Max Tokens"
                type="number"
                min={1}
                value={form.max_tokens}
                onChange={(e) => patch('max_tokens', Number(e.target.value))}
                hint="Ceiling on the completion length per LLM call."
              />
              <Input
                label="Token Budget / Task"
                type="number"
                min={1}
                value={form.token_budget_per_task}
                onChange={(e) => patch('token_budget_per_task', Number(e.target.value))}
                hint="Hard cap across the whole task (all LLM + tool-loop calls)."
              />
            </div>
          </Section>

          <Section title="System Prompt" desc="The standing instruction prepended to every task.">
            <Textarea
              value={form.system_prompt}
              onChange={(e) => patch('system_prompt', e.target.value)}
              className="min-h-[132px]"
              required
            />
            <p className="mt-1.5 text-right font-mono text-xs text-faint">{form.system_prompt.length} chars</p>
          </Section>

          <Section title="Safety & Memory" desc="The guardrail policy applied on input/output, and what the agent remembers.">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {policiesAvailable ? (
                <Select
                  label="Guardrail Policy"
                  value={form.guardrail_policy_id}
                  onChange={(e) => patch('guardrail_policy_id', e.target.value)}
                  hint="Runs on the PRE and POST guardrail stages. Default = the tenant policy."
                >
                  <option value="">— Tenant Default —</option>
                  {form.guardrail_policy_id && !policies.some((p) => p.policy_id === form.guardrail_policy_id) && (
                    <option value={form.guardrail_policy_id}>{shortId(form.guardrail_policy_id, 8)} (current)</option>
                  )}
                  {policies.map((p) => (
                    <option key={p.policy_id} value={p.policy_id}>
                      {p.name}
                      {p.is_default ? ' (default)' : ''}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  label="Guardrail Policy ID"
                  value={form.guardrail_policy_id}
                  onChange={(e) => patch('guardrail_policy_id', e.target.value)}
                  hint="Optional. Leave blank to use the tenant default policy."
                />
              )}

              <Select
                label="Memory Scope"
                value={form.memory_scope}
                onChange={(e) => patch('memory_scope', e.target.value as MemoryScope)}
                hint="What the agent recalls: none / agent / user / tenant / session."
              >
                {MEMORY_SCOPES.map((s) => (
                  <option key={s} value={s}>
                    {s === 'none' ? 'None (stateless)' : s.charAt(0).toUpperCase() + s.slice(1)}
                  </option>
                ))}
              </Select>
            </div>
          </Section>

          <Section title="Tools & Skills" desc="What the agent may call during the tool loop.">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Input
                label="Allowed Tools"
                value={form.allowed_tools}
                onChange={(e) => patch('allowed_tools', e.target.value)}
                hint="Comma-separated tool ids enabled for the TOOL_LOOP stage."
              />
              <Input
                label="Allowed Skills"
                value={form.allowed_skills}
                onChange={(e) => patch('allowed_skills', e.target.value)}
                hint="Comma-separated skill ids."
              />
            </div>
            <div className="mt-4 rounded-md border border-border bg-surface-2 px-3.5 py-3">
              <Switch
                checked={form.tool_loop_enabled}
                onChange={(v) => patch('tool_loop_enabled', v)}
                label="Multi-Call Tool Loop"
                hint="On = the full LLM↔tool loop runs (multiple LLM calls). Off = a single LLM call, tool loop skipped — use for rate-limited / free-tier models."
              />
            </div>
          </Section>

          <Section title="Knowledge (RAG)" desc="Knowledge bases retrieved before the prompt is built, and the retrieval knobs.">
            {kbsAvailable ? (
              <div className="flex flex-col gap-2">
                <p className="text-sm font-medium text-fg">Knowledge Bases</p>
                <div className="grid grid-cols-1 gap-px overflow-hidden rounded-md border border-border bg-border sm:grid-cols-2">
                  {kbs.map((kb) => {
                    const checked = form.allowed_kb_ids.includes(kb.kb_id);
                    return (
                      <label
                        key={kb.kb_id}
                        className={cn(
                          'flex cursor-pointer items-center gap-2.5 bg-surface px-3 py-2.5 transition-colors hover:bg-surface-2',
                          checked && 'bg-surface-2',
                        )}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleKb(kb.kb_id)}
                          className="h-4 w-4 accent-brand"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm text-fg">{kb.name}</span>
                          <span className="block truncate font-mono text-xs text-muted">{shortId(kb.kb_id, 12)}</span>
                        </span>
                        {typeof kb.chunk_count === 'number' && (
                          <span className="shrink-0 font-mono text-xs text-faint">{kb.chunk_count} chunks</span>
                        )}
                      </label>
                    );
                  })}
                </div>
                {form.allowed_kb_ids.length > 0 && (
                  <p className="text-xs text-muted">{form.allowed_kb_ids.length} selected</p>
                )}
              </div>
            ) : (
              <Input
                label="Knowledge Base IDs"
                value={form.allowed_kb_ids.join(', ')}
                onChange={(e) => patch('allowed_kb_ids', csv(e.target.value))}
                hint="Comma-separated knowledge-base ids for RAG retrieval."
              />
            )}

            <div className="mt-4 grid grid-cols-2 gap-4">
              <Input
                label="Top-K per KB"
                type="number"
                min={1}
                max={20}
                value={form.rag_top_k_per_kb}
                onChange={(e) => patch('rag_top_k_per_kb', Number(e.target.value))}
                hint="Chunks retrieved from each KB."
              />
              <div className="flex flex-col gap-1.5">
                <div className="flex items-baseline justify-between">
                  <label htmlFor="ab-minscore" className="text-sm font-medium text-fg">
                    Min Score
                  </label>
                  <span className="font-mono text-xs tabular-nums text-muted">{form.rag_min_score.toFixed(2)}</span>
                </div>
                <input
                  id="ab-minscore"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={form.rag_min_score}
                  onChange={(e) => patch('rag_min_score', Number(e.target.value))}
                  className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-brand"
                />
                <p className="text-xs text-muted">Relevance floor — drop chunks below this.</p>
              </div>
            </div>
          </Section>

          {error ? (
            <div className="px-4 pb-4">
              <ErrorBanner
                error={error}
                title={publishStep2Pending ? 'Activation (publish step 2) failed — config is saved' : undefined}
              />
            </div>
          ) : null}
        </form>
      </Card>
    </div>
  );
}
