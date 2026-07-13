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
  Input,
  Loading,
  Table,
  Textarea,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import { rerankDocuments, type RerankResult } from '@/lib/services';

interface Blocked {
  message: string;
  traceId?: string;
}

interface RankRow {
  rank: number; // 1-based position in the returned order
  index: number; // original input line index (0-based)
  text: string;
  score: number | undefined;
}

/** Rerank tester — score candidate documents against a query, ranked as the gateway returns them. */
export function RerankTester() {
  const [query, setQuery] = useState('');
  const [docsText, setDocsText] = useState('');
  const [topN, setTopN] = useState('');
  const [model, setModel] = useState('');

  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RerankResult | null>(null);
  // Snapshot of the documents actually submitted, so results map by index even if the box changes.
  const [submittedDocs, setSubmittedDocs] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [blocked, setBlocked] = useState<Blocked | null>(null);

  // One document per line; blank / whitespace-only lines are dropped.
  const docs = docsText
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  const canRun = query.trim().length > 0 && docs.length > 0;

  async function run(e: FormEvent) {
    e.preventDefault();
    if (!canRun) return;
    setRunning(true);
    setError(null);
    setBlocked(null);
    setResult(null);
    try {
      const n = Number(topN);
      const m = model.trim();
      const res = await rerankDocuments({
        query: query.trim(),
        documents: docs.map((text) => ({ text })),
        top_n: Number.isFinite(n) && n > 0 ? n : undefined,
        model: m || undefined,
      });
      setSubmittedDocs(docs);
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

  // Map each result back to its submitted document by `index`; keep the returned order.
  const rows: RankRow[] = (result?.results ?? []).map((r, i) => ({
    rank: i + 1,
    index: r.index,
    text: submittedDocs[r.index] ?? '—',
    score: r.relevance_score ?? r.score,
  }));

  const columns: Array<Column<RankRow>> = [
    {
      key: 'rank',
      header: '#',
      className: 'w-10 text-right tabular-nums',
      render: (r) => <span className="font-mono text-xs text-muted">{r.rank}</span>,
    },
    {
      key: 'line',
      header: 'Input',
      className: 'whitespace-nowrap',
      render: (r) => <span className="font-mono text-xs text-muted">Line {r.index + 1}</span>,
    },
    {
      key: 'doc',
      header: 'Document',
      render: (r) => <span className="text-sm text-fg">{r.text}</span>,
    },
    {
      key: 'score',
      header: 'Score',
      className: 'text-right',
      render: (r) => (
        <div className="flex justify-end">
          <Badge tone="info">
            <span className="font-mono tabular-nums">{r.score !== undefined ? r.score.toFixed(4) : '—'}</span>
          </Badge>
        </div>
      ),
    },
  ];

  const docHint = `One document per line; blank lines are ignored.${
    docs.length ? ` ${docs.length} document${docs.length === 1 ? '' : 's'} parsed.` : ''
  }`;

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
      {/* ── Composer ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader title="Composer" />
        <CardBody>
          <form onSubmit={run} className="flex flex-col gap-4">
            <Input
              label="Query"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="What should the documents be ranked against?"
              required
            />

            <Textarea
              label="Documents"
              value={docsText}
              onChange={(e) => setDocsText(e.target.value)}
              placeholder={'One document per line…'}
              hint={docHint}
              required
            />

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Input
                label="Top N"
                type="number"
                min={1}
                value={topN}
                onChange={(e) => setTopN(e.target.value)}
                placeholder="All"
                hint="Optional — cap how many results come back."
              />
              <Input
                label="Model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="e.g. rerank-default or a model id"
                hint="Optional — gateway default when blank."
              />
            </div>

            <div>
              <Button type="submit" size="md" loading={running} disabled={!canRun}>
                Rerank
              </Button>
            </div>
          </form>
        </CardBody>
      </Card>

      {/* ── Ranking ──────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Ranking"
          description={result?.model ? <span className="font-mono text-xs">{result.model}</span> : undefined}
        />
        <CardBody className="px-0 py-0">
          {running ? (
            <Loading label="Reranking documents…" />
          ) : blocked ? (
            <div className="p-4">
              <Callout tone="danger" title="Blocked by guardrails">
                <p>{blocked.message}</p>
                {blocked.traceId ? (
                  <p className="mt-1 font-mono text-xs text-muted">trace: {blocked.traceId}</p>
                ) : null}
              </Callout>
            </div>
          ) : error ? (
            <div className="p-4">
              <ErrorBanner error={error} title="Rerank failed" />
            </div>
          ) : result ? (
            <Table
              columns={columns}
              rows={rows}
              rowKey={(r) => `${r.rank}-${r.index}`}
              empty="The reranker returned no results."
            />
          ) : (
            <div className="p-4">
              <EmptyState title="No Ranking Yet" description="Rerank documents to order them by relevance." />
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
