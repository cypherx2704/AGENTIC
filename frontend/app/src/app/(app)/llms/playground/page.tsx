'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorBanner,
  humanizeStatus,
  Input,
  Loading,
  Select,
  Stat,
  Textarea,
} from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import {
  chatCompletion,
  listAliases,
  listModels,
  type ChatCompletionResult,
  type ChatMessage,
  type LlmAlias,
} from '@/lib/services';
import type { LlmModel } from '@/lib/types';
import { useAsync } from '@/lib/useAsync';
import { cn, formatNumber } from '@/lib/utils';
import { EmbeddingsTester } from './EmbeddingsTester';
import { ClassifyTester } from './ClassifyTester';
import { RerankTester } from './RerankTester';

// Sentinel Select value that reveals a free-text model Input — lets an operator test any
// concrete model id, alias, or BYOK model even when it isn't in the loaded lists.
const CUSTOM = '__custom__';

type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

interface PickerData {
  aliases: LlmAlias[];
  models: LlmModel[];
}

interface Blocked {
  message: string;
  code: string;
  traceId?: string;
}

// Load aliases + models independently so one failing endpoint still populates the other.
async function loadPickerData(signal: AbortSignal): Promise<PickerData> {
  const [aliasesR, modelsR] = await Promise.allSettled([listAliases({}, signal), listModels(signal)]);
  return {
    aliases: aliasesR.status === 'fulfilled' ? aliasesR.value.data ?? [] : [],
    models: modelsR.status === 'fulfilled' ? modelsR.value.data ?? [] : [],
  };
}

// finish_reason → semantic badge tone.
function finishTone(reason: string | undefined): BadgeTone {
  switch (reason) {
    case 'stop':
      return 'success';
    case 'length':
      return 'warning';
    case 'content_filter':
      return 'danger';
    case 'tool_calls':
      return 'info';
    default:
      return 'neutral';
  }
}

// ── Chat tester (the original playground composer/result, preserved verbatim) ─────────────
function ChatTester() {
  const { data, loading } = useAsync(loadPickerData, []);

  const [model, setModel] = useState('');
  const [customModel, setCustomModel] = useState('');
  const [system, setSystem] = useState('');
  const [user, setUser] = useState('');
  const [maxTokens, setMaxTokens] = useState('512');
  const [temperature, setTemperature] = useState(0.7);

  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ChatCompletionResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [blocked, setBlocked] = useState<Blocked | null>(null);

  const aliases = data?.aliases ?? [];
  const models = data?.models ?? [];
  const hasOptions = aliases.length + models.length > 0;

  // Once the picker loads, pre-select the default alias (else first alias / first model). With
  // no options at all we leave `model` empty and fall through to the free-text Input.
  useEffect(() => {
    if (!data || model !== '') return;
    const def = aliases.find((a) => a.is_default) ?? aliases[0];
    if (def) setModel(def.alias);
    else if (models[0]) setModel(models[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const usingCustom = !hasOptions || model === CUSTOM;
  const effectiveModel = (usingCustom ? customModel : model).trim();
  const canRun = effectiveModel.length > 0 && user.trim().length > 0;

  async function run(e: FormEvent) {
    e.preventDefault();
    if (!canRun) return;
    setRunning(true);
    setError(null);
    setBlocked(null);
    setResult(null);
    try {
      const messages: ChatMessage[] = [];
      const sys = system.trim();
      if (sys) messages.push({ role: 'system', content: sys });
      messages.push({ role: 'user', content: user });

      const mt = Number(maxTokens);
      const res = await chatCompletion({
        model: effectiveModel,
        messages,
        max_tokens: Number.isFinite(mt) && mt > 0 ? mt : undefined,
        temperature,
      });
      setResult(res);
    } catch (err) {
      // A guardrail block is a 422 GUARDRAIL_VIOLATION — surface it as a clear danger Callout
      // rather than a generic error banner.
      if (err instanceof BffError && err.isGuardrailViolation) {
        setBlocked({ message: err.message, code: err.code, traceId: err.traceId });
      } else {
        setError(err);
      }
    } finally {
      setRunning(false);
    }
  }

  const choice = result?.choices?.[0];
  const content = choice?.message?.content;
  const finish = choice?.finish_reason;
  const resultModel = result?.model;

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      {/* ── Composer ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader title="Composer" />
        <CardBody>
          <form onSubmit={run} className="flex flex-col gap-4">
            {loading && !data ? (
              <Select label="Model" disabled hint="Loading models &amp; aliases…">
                <option>Loading…</option>
              </Select>
            ) : hasOptions ? (
              <Select
                label="Model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                hint="Pick an alias or a concrete model id — both route through the gateway."
              >
                {aliases.length > 0 && (
                  <optgroup label="Aliases">
                    {aliases.map((a, i) => (
                      <option key={`alias-${i}-${a.alias}`} value={a.alias}>
                        {a.alias} — {a.model_id}
                        {a.is_default ? ' (default)' : ''}
                      </option>
                    ))}
                  </optgroup>
                )}
                {models.length > 0 && (
                  <optgroup label="Models">
                    {models.map((m, i) => (
                      <option key={`model-${i}-${m.id}`} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                  </optgroup>
                )}
                <option value={CUSTOM}>Custom model…</option>
              </Select>
            ) : (
              <Input
                label="Model"
                value={customModel}
                onChange={(e) => setCustomModel(e.target.value)}
                placeholder="e.g. smart, small, or a concrete model id"
                hint="No models or aliases found — enter a model id, alias, or BYOK model."
                required
              />
            )}

            {hasOptions && model === CUSTOM && (
              <Input
                label="Custom Model"
                value={customModel}
                onChange={(e) => setCustomModel(e.target.value)}
                placeholder="e.g. anthropic/claude-… or an alias"
                required
              />
            )}

            <Textarea
              label="System Prompt"
              value={system}
              onChange={(e) => setSystem(e.target.value)}
              placeholder="Optional — set the assistant's behavior."
              hint="Sent as the leading system message when non-empty."
            />

            <Textarea
              label="User Message"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              placeholder="Ask something… (try a prompt-injection to see the guardrail block)."
              required
            />

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Input
                label="Max Tokens"
                type="number"
                min={1}
                value={maxTokens}
                onChange={(e) => setMaxTokens(e.target.value)}
                hint="Upper bound on the completion length."
              />
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <label htmlFor="pg-temp" className="text-sm font-medium text-fg">
                    Temperature
                  </label>
                  <span className="font-mono text-xs tabular-nums text-muted">{temperature.toFixed(1)}</span>
                </div>
                <input
                  id="pg-temp"
                  type="range"
                  min={0}
                  max={2}
                  step={0.1}
                  value={temperature}
                  onChange={(e) => setTemperature(Number(e.target.value))}
                  className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-surface-2 accent-brand"
                />
                <p className="text-xs text-muted">0 = deterministic · 2 = most creative.</p>
              </div>
            </div>

            <div>
              <Button type="submit" size="md" loading={running} disabled={!canRun}>
                Run
              </Button>
            </div>
          </form>
        </CardBody>
      </Card>

      {/* ── Result ───────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Result"
          description={resultModel ? <span className="font-mono text-xs">{resultModel}</span> : undefined}
          actions={finish ? <Badge tone={finishTone(finish)}>{humanizeStatus(finish)}</Badge> : undefined}
        />
        <CardBody>
          {running ? (
            <Loading label="Running completion…" />
          ) : blocked ? (
            <Callout tone="danger" title="Blocked by guardrails">
              <p>{blocked.message}</p>
              {blocked.traceId ? <p className="mt-1 font-mono text-xs text-muted">trace: {blocked.traceId}</p> : null}
            </Callout>
          ) : error ? (
            <ErrorBanner error={error} title="Completion failed" />
          ) : result ? (
            <div className="flex flex-col gap-4">
              <div>
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">Response</p>
                <div className="rounded-md border border-border bg-surface-2 px-4 py-3">
                  {content ? (
                    <p className="select-text whitespace-pre-wrap text-sm text-fg">{content}</p>
                  ) : (
                    <p className="text-sm text-muted">The model returned no text content.</p>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <Stat label="Prompt Tokens" value={formatNumber(result.usage?.prompt_tokens)} />
                <Stat label="Completion Tokens" value={formatNumber(result.usage?.completion_tokens)} />
                <Stat label="Total Tokens" value={formatNumber(result.usage?.total_tokens)} />
              </div>
            </div>
          ) : (
            <EmptyState title="No Response Yet" description="Run a completion to see the response." />
          )}
        </CardBody>
      </Card>
    </div>
  );
}

// ── Tab strip ─────────────────────────────────────────────────────────────────────────────
type TabKey = 'chat' | 'embeddings' | 'classify' | 'rerank';

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'chat', label: 'Chat' },
  { key: 'embeddings', label: 'Embeddings' },
  { key: 'classify', label: 'Classify' },
  { key: 'rerank', label: 'Rerank' },
];

export default function PlaygroundPage() {
  const [tab, setTab] = useState<TabKey>('chat');

  return (
    <Page>
      <PageHeader
        title="Playground"
        description="Send chat, embeddings, classify, and rerank requests through the gateway to test a model, alias, BYOK key, or rule."
      />

      <PageBody>
        <div className="mb-4 flex items-center gap-1 border-b border-border">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              aria-current={tab === t.key ? 'page' : undefined}
              className={cn(
                'relative -mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                tab === t.key ? 'border-brand text-fg-strong' : 'border-transparent text-muted hover:text-fg',
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === 'chat' ? <ChatTester /> : null}
        {tab === 'embeddings' ? <EmbeddingsTester /> : null}
        {tab === 'classify' ? <ClassifyTester /> : null}
        {tab === 'rerank' ? <RerankTester /> : null}
      </PageBody>
    </Page>
  );
}
