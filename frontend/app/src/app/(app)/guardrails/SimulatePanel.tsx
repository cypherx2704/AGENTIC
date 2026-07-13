'use client';

import { useState, type FormEvent } from 'react';
import { Badge, Button, ErrorBanner, Input, Modal, Select, Table, Textarea } from '@/components/ui';
import type { Column } from '@/components/ui';
import { simulateDraftPolicy, simulateStoredPolicy } from '@/lib/services';
import type { SimulationResult } from '@/lib/types';

/** What to simulate against: a stored (persisted) policy, or an inline unsaved draft. */
export type SimulateTarget =
  | { kind: 'stored'; policyId: string; policyName: string }
  | {
      kind: 'draft';
      name: string;
      rules: Array<{ rule_id: string; enabled: boolean; action_override?: string | null }>;
      failModeOverride?: string | null;
    };

type Direction = 'input' | 'output';
type Tone = 'neutral' | 'success' | 'warning' | 'info' | 'danger';

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

// ── Safe readers for the open-ended evaluation_trace entries (Record<string, unknown>) ──
function asStr(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined;
}
function asNum(v: unknown): number | undefined {
  return typeof v === 'number' ? v : undefined;
}

interface TraceView {
  rule_id?: string;
  rule_name?: string;
  direction?: string;
  evaluated: boolean;
  matched: boolean;
  action?: string;
  effective_fail_mode?: string;
  timed_out: boolean;
  timing_ms?: number;
  hit_count?: number;
}

function toView(e: Record<string, unknown>): TraceView {
  return {
    rule_id: asStr(e.rule_id),
    rule_name: asStr(e.rule_name),
    direction: asStr(e.direction),
    evaluated: e.evaluated === true,
    matched: e.matched === true,
    action: asStr(e.action),
    effective_fail_mode: asStr(e.effective_fail_mode),
    timed_out: e.timed_out === true,
    timing_ms: asNum(e.timing_ms),
    hit_count: asNum(e.hit_count),
  };
}

/**
 * Simulate a policy against sample text — no violation is persisted. Reused by the Policies
 * tab (stored policy) and available for inline drafts. Renders the decision + a per-rule
 * evaluation trace.
 */
export function SimulatePanel({ target, onClose }: { target: SimulateTarget; onClose: () => void }) {
  const [direction, setDirection] = useState<Direction>('input');
  const [text, setText] = useState('');
  const [inputText, setInputText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<SimulationResult | null>(null);

  const title = target.kind === 'stored' ? 'Simulate Policy' : 'Simulate Draft Policy';
  const subject = target.kind === 'stored' ? target.policyName : target.name;

  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const base = {
        text,
        input_text: direction === 'output' ? inputText.trim() || undefined : undefined,
        direction,
      };
      const res =
        target.kind === 'stored'
          ? await simulateStoredPolicy(target.policyId, base)
          : await simulateDraftPolicy({
              ...base,
              rules: target.rules,
              fail_mode_override: target.failModeOverride ?? null,
            });
      setResult(res);
    } catch (err) {
      setError(err);
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  const traceRows = (result?.evaluation_trace ?? []).map(toView);
  const matched = traceRows.filter((t) => t.matched).length;

  const columns: Array<Column<TraceView>> = [
    {
      key: 'rule',
      header: 'Rule',
      render: (t) => <span className="font-mono text-xs text-fg">{t.rule_name ?? t.rule_id ?? '—'}</span>,
    },
    {
      key: 'direction',
      header: 'Direction',
      render: (t) => <span className="text-xs uppercase text-muted">{t.direction ?? '—'}</span>,
    },
    {
      key: 'evaluated',
      header: 'Evaluated',
      render: (t) => <Badge tone={t.evaluated ? 'info' : 'neutral'}>{t.evaluated ? 'Yes' : 'Skipped'}</Badge>,
    },
    {
      key: 'matched',
      header: 'Matched',
      render: (t) =>
        t.matched ? <Badge tone="warning">Matched</Badge> : <span className="text-xs text-muted">—</span>,
    },
    {
      key: 'action',
      header: 'Action',
      render: (t) => (t.action ? <Badge tone={decisionTone(t.action)}>{titleCase(t.action)}</Badge> : <span className="text-muted">—</span>),
    },
    {
      key: 'fail_mode',
      header: 'Fail Mode',
      render: (t) => (
        <span className="text-xs text-muted">
          {t.effective_fail_mode ?? '—'}
          {t.timed_out ? ' · timeout' : ''}
        </span>
      ),
    },
    {
      key: 'timing',
      header: 'Timing',
      className: 'text-right',
      render: (t) => (
        <span className="font-mono text-xs tabular-nums text-muted">{t.timing_ms != null ? `${t.timing_ms} ms` : '—'}</span>
      ),
    },
    {
      key: 'hits',
      header: 'Hits',
      className: 'text-right',
      render: (t) => <span className="font-mono text-xs tabular-nums text-fg">{t.hit_count ?? 0}</span>,
    },
  ];

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={title}
      description={subject}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Close
          </Button>
          <Button form="simulate-form" type="submit" loading={busy} disabled={!text.trim()}>
            Run Simulation
          </Button>
        </>
      }
    >
      <form id="simulate-form" onSubmit={run} className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Select label="Direction" value={direction} onChange={(e) => setDirection(e.target.value as Direction)}>
            <option value="input">Input</option>
            <option value="output">Output</option>
          </Select>
          {direction === 'output' ? (
            <Input label="Input Text (Original Prompt)" value={inputText} onChange={(e) => setInputText(e.target.value)} />
          ) : (
            <div />
          )}
        </div>
        <Textarea
          label="Text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          required
          placeholder="Sample text to evaluate…"
        />
        {error ? <ErrorBanner error={error} title="Simulation failed" /> : null}
      </form>

      {result ? (
        <div className="mt-4 flex flex-col gap-3 border-t border-border pt-4">
          <div className="flex flex-wrap items-center gap-4">
            <Badge tone={decisionTone(result.decision)} className="px-3 py-1 text-sm">
              {titleCase(result.decision)}
            </Badge>
            <span className="text-xs text-muted">
              {matched} matched · {traceRows.length} evaluated
              {result.duration_ms != null ? ` · ${result.duration_ms} ms` : ''}
            </span>
          </div>
          <div className="max-h-72 overflow-auto rounded-md border border-border">
            <Table
              columns={columns}
              rows={traceRows}
              rowKey={(t, i) => `${t.rule_id ?? 'rule'}-${i}`}
              empty="No rules were evaluated."
            />
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
