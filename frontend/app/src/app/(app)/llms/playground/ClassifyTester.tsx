'use client';

import { useState, type FormEvent } from 'react';
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
  Textarea,
} from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import { classifyText, type ClassifyResult } from '@/lib/services';

type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

interface Blocked {
  message: string;
  traceId?: string;
}

interface CategoryRow {
  category: string;
  score: number;
}

// Verdict strings vary by classifier — match defensively on the common safe / unsafe vocabularies.
const SAFE_VERDICTS = new Set([
  'safe',
  'benign',
  'ok',
  'pass',
  'passed',
  'clean',
  'allow',
  'allowed',
  'non_toxic',
  'nontoxic',
  'not_flagged',
]);
const UNSAFE_VERDICTS = new Set([
  'toxic',
  'unsafe',
  'flagged',
  'block',
  'blocked',
  'violation',
  'harmful',
  'fail',
  'failed',
  'danger',
  'dangerous',
  'reject',
  'rejected',
  'denied',
]);

function verdictTone(verdict: string | undefined): BadgeTone {
  const v = (verdict ?? '').trim().toLowerCase();
  if (!v) return 'neutral';
  if (SAFE_VERDICTS.has(v)) return 'success';
  if (UNSAFE_VERDICTS.has(v)) return 'danger';
  return 'neutral';
}

// Normalize both wire shapes — Record<string, number> and Array<{category, score}> — to sorted rows.
function toCategoryRows(categories: ClassifyResult['categories']): CategoryRow[] {
  if (!categories) return [];
  const rows: CategoryRow[] = Array.isArray(categories)
    ? categories.map((c) => ({ category: String(c.category), score: Number(c.score) }))
    : Object.entries(categories).map(([category, score]) => ({ category, score: Number(score) }));
  return rows.filter((r) => Number.isFinite(r.score)).sort((a, b) => b.score - a.score);
}

/** Classify tester — safety-classify a string, showing the verdict + per-category scores. */
export function ClassifyTester() {
  const [input, setInput] = useState('');
  const [model, setModel] = useState('');

  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ClassifyResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [blocked, setBlocked] = useState<Blocked | null>(null);

  const canRun = input.trim().length > 0;

  async function run(e: FormEvent) {
    e.preventDefault();
    if (!canRun) return;
    setRunning(true);
    setError(null);
    setBlocked(null);
    setResult(null);
    try {
      const m = model.trim();
      const res = await classifyText({ input, model: m || undefined });
      setResult(res);
    } catch (err) {
      if (err instanceof BffError && err.isGuardrailViolation) {
        setBlocked({ message: err.message, traceId: err.traceId });
      } else {
        setError(err);
      }
    } finally {
      setRunning(false);
    }
  }

  const rows = result ? toCategoryRows(result.categories) : [];

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      {/* ── Composer ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader title="Composer" />
        <CardBody>
          <form onSubmit={run} className="flex flex-col gap-4">
            <Textarea
              label="Input Text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Text to classify…"
              hint="Scored by the safety classifier for a verdict and per-category signals."
              required
            />

            <Input
              label="Model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="e.g. safety-default or a concrete model id"
              hint="Optional — leave blank to use the gateway's default classifier alias."
            />

            <div>
              <Button type="submit" size="md" loading={running} disabled={!canRun}>
                Classify
              </Button>
            </div>
          </form>
        </CardBody>
      </Card>

      {/* ── Result ───────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Result"
          description={result?.model ? <span className="font-mono text-xs">{result.model}</span> : undefined}
        />
        <CardBody>
          {running ? (
            <Loading label="Classifying…" />
          ) : blocked ? (
            <Callout tone="danger" title="Blocked by guardrails">
              <p>{blocked.message}</p>
              {blocked.traceId ? <p className="mt-1 font-mono text-xs text-muted">trace: {blocked.traceId}</p> : null}
            </Callout>
          ) : error ? (
            <ErrorBanner error={error} title="Classification failed" />
          ) : result ? (
            <div className="flex flex-col gap-4">
              <div>
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">Verdict</p>
                {result.verdict ? (
                  <Badge tone={verdictTone(result.verdict)}>{humanizeStatus(result.verdict)}</Badge>
                ) : (
                  <span className="text-sm text-muted">No verdict returned.</span>
                )}
              </div>

              <div>
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-faint">Category Scores</p>
                {rows.length > 0 ? (
                  <div className="flex flex-col gap-2.5">
                    {rows.map((r) => {
                      const pct = Math.max(0, Math.min(1, r.score)) * 100;
                      return (
                        <div key={r.category}>
                          <div className="mb-1 flex items-center justify-between gap-3">
                            <span className="truncate text-sm text-fg">{humanizeStatus(r.category)}</span>
                            <span className="shrink-0 font-mono text-xs tabular-nums text-muted">
                              {r.score.toFixed(4)}
                            </span>
                          </div>
                          <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
                            <div className="h-full rounded-full bg-brand" style={{ width: `${pct}%` }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-sm text-muted">No category scores returned.</p>
                )}
              </div>
            </div>
          ) : (
            <EmptyState title="No Verdict Yet" description="Classify some text to see its verdict and scores." />
          )}
        </CardBody>
      </Card>
    </div>
  );
}
