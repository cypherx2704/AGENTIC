'use client';

import { useState, type FormEvent } from 'react';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyButton,
  ErrorBanner,
  Input,
  StatusBadge,
  Table,
  Textarea,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { checkInput, checkOutput } from '@/lib/services';
import type { CheckResult, CheckViolation } from '@/lib/types';
import { cn } from '@/lib/utils';

type Direction = 'input' | 'output';
type Tone = 'neutral' | 'success' | 'warning' | 'info' | 'danger';

/** Decision → semantic tone (allow=success, warn=warning, redact=info, block=danger). */
function decisionTone(d: string | undefined): Tone {
  switch (d) {
    case 'allow':
      return 'success';
    case 'warn':
      return 'warning';
    case 'redact':
      return 'info';
    case 'block':
      return 'danger';
    default:
      return 'neutral';
  }
}

function titleCase(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** The prominent decision chip, shared by the playground + the simulate panel semantics. */
export function DecisionBadge({ decision, className }: { decision: string; className?: string }) {
  return (
    <Badge tone={decisionTone(decision)} className={className}>
      {titleCase(decision)}
    </Badge>
  );
}

/** Parse a comma-separated field into a trimmed, non-empty string list (or undefined). */
function parseList(raw: string): string[] | undefined {
  const items = raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length ? items : undefined;
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className="mt-0.5 text-sm font-semibold tabular-nums text-fg-strong">{value}</p>
    </div>
  );
}

/**
 * The flagship "test guardrails without an agent" surface — runs sample text through the
 * live effective policy (input or output direction) and renders the decision + violations.
 */
export function CheckPlayground() {
  const [direction, setDirection] = useState<Direction>('input');
  const [text, setText] = useState('');
  const [inputText, setInputText] = useState('');
  const [contextRaw, setContextRaw] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<CheckResult | null>(null);

  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res =
        direction === 'input'
          ? await checkInput({ text, untrusted_spans: parseList(contextRaw) })
          : await checkOutput({
              text,
              input_text: inputText.trim() || undefined,
              grounding: parseList(contextRaw),
            });
      setResult(res);
    } catch (err) {
      setError(err);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  function pickDirection(d: Direction) {
    setDirection(d);
    setResult(null);
    setError(null);
  }

  const violationColumns: Array<Column<CheckViolation>> = [
    {
      key: 'rule',
      header: 'Rule',
      render: (v) => <span className="font-mono text-xs text-fg">{v.rule_name ?? v.rule_id ?? '—'}</span>,
    },
    { key: 'category', header: 'Category', render: (v) => <span className="text-sm text-fg">{v.category ?? '—'}</span> },
    {
      key: 'severity',
      header: 'Severity',
      render: (v) => (v.severity ? <StatusBadge status={v.severity} /> : <span className="text-muted">—</span>),
    },
    {
      key: 'matched',
      header: 'Matched (Safe)',
      render: (v) => <span className="font-mono text-xs text-muted">{v.matched ?? '—'}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardHeader
          title="Test Playground"
          description="Run text through the live effective policy — no agent required."
        />
        <CardBody>
          <form onSubmit={run} className="flex flex-col gap-4">
            <div>
              <p className="mb-1.5 text-sm font-medium text-fg">Direction</p>
              <div className="inline-flex rounded-md border border-border p-0.5">
                {(['input', 'output'] as Direction[]).map((d) => (
                  <button
                    key={d}
                    type="button"
                    onClick={() => pickDirection(d)}
                    aria-pressed={direction === d}
                    className={cn(
                      'rounded px-3 py-1 text-sm font-medium transition-colors',
                      direction === d ? 'bg-surface-2 text-fg-strong' : 'text-muted hover:text-fg',
                    )}
                  >
                    {d === 'input' ? 'Input' : 'Output'}
                  </button>
                ))}
              </div>
            </div>

            <Textarea
              label="Text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              required
              placeholder={direction === 'input' ? 'Prompt text to check…' : 'Model output to check…'}
            />

            {direction === 'output' ? (
              <Input
                label="Input Text (Original Prompt)"
                hint="Optional — the prompt that produced this output; enables 'echoed vs. new' PII logic."
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
              />
            ) : null}

            <Input
              label={direction === 'input' ? 'Untrusted Spans' : 'Grounding Passages'}
              hint={
                direction === 'input'
                  ? 'Optional, comma-separated. RAG/tool spans within the text to spotlight.'
                  : 'Optional, comma-separated. Context passages the output should be grounded in.'
              }
              value={contextRaw}
              onChange={(e) => setContextRaw(e.target.value)}
            />

            {error ? <ErrorBanner error={error} title="Check failed" /> : null}

            <div className="flex justify-end">
              <Button type="submit" loading={busy} disabled={!text.trim()}>
                Run Check
              </Button>
            </div>
          </form>
        </CardBody>
      </Card>

      {result ? (
        <Card>
          <CardHeader
            title="Result"
            actions={
              result.check_id ? (
                <CopyButton value={result.check_id} label="Copy Check ID" />
              ) : undefined
            }
          />
          <CardBody className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center gap-5">
              <DecisionBadge decision={result.decision} className="px-3 py-1 text-sm" />
              <Meta label="Duration" value={result.duration_ms != null ? `${result.duration_ms} ms` : '—'} />
              <Meta label="Confidence" value={result.confidence != null ? result.confidence.toFixed(3) : '—'} />
              <Meta label="Violations" value={String(result.violations.length)} />
            </div>

            {result.violations.length > 0 ? (
              <Table
                columns={violationColumns}
                rows={result.violations}
                rowKey={(v, i) => `${v.rule_id ?? 'violation'}-${i}`}
                empty="No violations."
              />
            ) : (
              <p className="text-sm text-muted">No rules fired — the text passed clean.</p>
            )}

            {result.decision === 'redact' && result.processed_text != null ? (
              <div>
                <p className="mb-1.5 text-sm font-medium text-fg">Processed Text (Redacted)</p>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-3 font-mono text-xs text-fg">
                  {result.processed_text}
                </pre>
              </div>
            ) : null}
          </CardBody>
        </Card>
      ) : null}
    </div>
  );
}
