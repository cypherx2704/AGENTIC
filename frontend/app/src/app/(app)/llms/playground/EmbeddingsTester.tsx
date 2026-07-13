'use client';

import { useState, type FormEvent } from 'react';
import {
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorBanner,
  Input,
  Loading,
  Stat,
  Textarea,
} from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import { createEmbeddings, type EmbeddingResult } from '@/lib/services';
import { formatNumber } from '@/lib/utils';

interface Blocked {
  message: string;
  traceId?: string;
}

// How many leading vector components to render as a preview (the rest are elided).
const PREVIEW_COUNT = 12;

/** Embeddings tester — embed one string, show its dimension, a vector preview, and token usage. */
export function EmbeddingsTester() {
  const [input, setInput] = useState('');
  const [model, setModel] = useState('');

  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<EmbeddingResult | null>(null);
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
      const res = await createEmbeddings({ input, model: m || undefined });
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

  // Fields are all optional on the wire — read the first vector defensively.
  const vector = result?.data?.[0]?.embedding;
  const dimension = vector?.length;
  const preview = vector?.slice(0, PREVIEW_COUNT);

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
              placeholder="Text to embed…"
              hint="Sent as a single string to the embeddings endpoint."
              required
            />

            <Input
              label="Model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="e.g. embed or a concrete model id"
              hint="Optional — leave blank to use the gateway's default embedding alias."
            />

            <div>
              <Button type="submit" size="md" loading={running} disabled={!canRun}>
                Embed
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
            <Loading label="Creating embedding…" />
          ) : blocked ? (
            <Callout tone="danger" title="Blocked by guardrails">
              <p>{blocked.message}</p>
              {blocked.traceId ? <p className="mt-1 font-mono text-xs text-muted">trace: {blocked.traceId}</p> : null}
            </Callout>
          ) : error ? (
            <ErrorBanner error={error} title="Embedding failed" />
          ) : result ? (
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-3 gap-3">
                <Stat label="Dimensions" value={dimension !== undefined ? formatNumber(dimension) : '—'} />
                <Stat label="Prompt Tokens" value={formatNumber(result.usage?.prompt_tokens)} />
                <Stat label="Total Tokens" value={formatNumber(result.usage?.total_tokens)} />
              </div>

              <div>
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">
                  Vector Preview
                  {dimension !== undefined ? ` (first ${Math.min(PREVIEW_COUNT, dimension)} of ${dimension})` : ''}
                </p>
                <div className="overflow-x-auto rounded-md border border-border bg-surface-2 px-4 py-3">
                  {preview && preview.length > 0 ? (
                    <p className="select-text whitespace-nowrap font-mono text-xs tabular-nums text-fg">
                      [{preview.map((n) => n.toFixed(6)).join(', ')}
                      {dimension !== undefined && dimension > PREVIEW_COUNT ? ', …' : ''}]
                    </p>
                  ) : (
                    <p className="text-sm text-muted">The response contained no vector data.</p>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <EmptyState title="No Embedding Yet" description="Embed some text to see its vector." />
          )}
        </CardBody>
      </Card>
    </div>
  );
}
