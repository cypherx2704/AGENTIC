'use client';

import { useMemo, useState } from 'react';
import { Button, Card, CardBody, CardHeader, ErrorBanner, Input, Select, Textarea, useToast } from '@/components/ui';
import { putRuntime } from '@/lib/services';
import type { AgentRuntime, AgentRuntimeRegistration, AgentRuntimeStatus, LlmModel, MemoryScope } from '@/lib/types';

// The full memory_scope enum — including "session" — matching the xAgent runtime model.
const MEMORY_SCOPES: MemoryScope[] = ['none', 'agent', 'user', 'tenant', 'session'];
const STATUSES: AgentRuntimeStatus[] = ['pending_config', 'active', 'inactive'];

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
  allowed_kb_ids: string;
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
    allowed_kb_ids: (rt?.allowed_kb_ids ?? []).join(', '),
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
    allowed_kb_ids: csv(f.allowed_kb_ids),
    rag_top_k_per_kb: Number(f.rag_top_k_per_kb),
    rag_min_score: Number(f.rag_min_score),
    token_budget_per_task: Number(f.token_budget_per_task),
  };
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
  onSaved,
}: {
  agentId: string;
  fallbackName: string;
  initialRuntime: AgentRuntime | null;
  models: LlmModel[];
  onSaved: (rt: AgentRuntime) => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<FormState>(() => toForm(initialRuntime, fallbackName));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Two-step publish tracking: after a successful step-1 save we may still need step 2.
  const [publishStep2Pending, setPublishStep2Pending] = useState(false);

  function patch<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
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

  /** Save the runtime config only (no status flip beyond what's in the form). */
  async function save(statusOverride?: AgentRuntimeStatus) {
    setSaving(true);
    setError(null);
    const status = statusOverride ?? form.status;
    const rt = await putRuntime(agentId, toRegistration(form, status));
    setForm(toForm(rt, fallbackName));
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
    <Card>
      <CardHeader
        title="Agent Builder"
        description="Create or edit the xAgent runtime. Publish activates the agent so it can run tasks."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={onSaveDraft} loading={saving && !publishStep2Pending}>
              Save config
            </Button>
            {publishStep2Pending ? (
              <Button size="sm" onClick={onRetryPublish} loading={saving}>
                Retry publish (step 2)
              </Button>
            ) : (
              <Button size="sm" onClick={onPublish} loading={saving}>
                Publish
              </Button>
            )}
          </div>
        }
      />
      <CardBody>
        <form onSubmit={onSaveDraft} className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Input label="Display name" value={form.name} onChange={(e) => patch('name', e.target.value)} required />

          <Select label="Status" value={form.status} onChange={(e) => patch('status', e.target.value as AgentRuntimeStatus)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>

          <Select
            label="LLM model"
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

          <Select
            label="Memory scope"
            value={form.memory_scope}
            onChange={(e) => patch('memory_scope', e.target.value as MemoryScope)}
            hint="Full enum — none / agent / user / tenant / session."
          >
            {MEMORY_SCOPES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>

          <div className="md:col-span-2">
            <Textarea
              label="System prompt"
              value={form.system_prompt}
              onChange={(e) => patch('system_prompt', e.target.value)}
              required
            />
          </div>

          <Input
            label="Max tokens"
            type="number"
            min={1}
            value={form.max_tokens}
            onChange={(e) => patch('max_tokens', Number(e.target.value))}
          />
          <Input
            label="Temperature"
            type="number"
            step="0.1"
            min={0}
            max={2}
            value={form.temperature}
            onChange={(e) => patch('temperature', Number(e.target.value))}
          />

          <Input
            label="Token budget per task"
            type="number"
            min={1}
            value={form.token_budget_per_task}
            onChange={(e) => patch('token_budget_per_task', Number(e.target.value))}
          />
          <Input
            label="Guardrail policy id"
            value={form.guardrail_policy_id}
            onChange={(e) => patch('guardrail_policy_id', e.target.value)}
            hint="Optional. Leave blank to use the tenant default policy."
          />

          <Input
            label="Allowed tools"
            value={form.allowed_tools}
            onChange={(e) => patch('allowed_tools', e.target.value)}
            hint="Comma-separated tool ids enabled for the TOOL_LOOP stage."
          />
          <Select
            label="Tool execution mode"
            value={form.tool_loop_enabled ? 'multiple' : 'single'}
            onChange={(e) => patch('tool_loop_enabled', e.target.value === 'multiple')}
            hint="Multiple request = full tool loop (many LLM calls). Per request = one LLM call, tool loop skipped (use for rate-limited / free-tier models)."
          >
            <option value="multiple">Multiple request (tool loop)</option>
            <option value="single">Per request (single LLM call)</option>
          </Select>

          <Input
            label="Allowed skills"
            value={form.allowed_skills}
            onChange={(e) => patch('allowed_skills', e.target.value)}
            hint="Comma-separated skill ids."
          />

          <Input
            label="Allowed KB ids"
            value={form.allowed_kb_ids}
            onChange={(e) => patch('allowed_kb_ids', e.target.value)}
            hint="Comma-separated knowledge-base ids for RAG retrieval."
          />
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="RAG top-k / KB"
              type="number"
              min={1}
              max={20}
              value={form.rag_top_k_per_kb}
              onChange={(e) => patch('rag_top_k_per_kb', Number(e.target.value))}
            />
            <Input
              label="RAG min score"
              type="number"
              step="0.05"
              min={0}
              max={1}
              value={form.rag_min_score}
              onChange={(e) => patch('rag_min_score', Number(e.target.value))}
            />
          </div>

          {error ? (
            <div className="md:col-span-2">
              <ErrorBanner
                error={error}
                title={publishStep2Pending ? 'Activation (publish step 2) failed — config is saved' : undefined}
              />
            </div>
          ) : null}
        </form>
      </CardBody>
    </Card>
  );
}
