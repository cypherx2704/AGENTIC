'use client';

import { useState } from 'react';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyButton,
  ErrorBanner,
  Input,
  Select,
  Switch,
  Textarea,
} from '@/components/ui';
import { queryKnowledgeBase } from '@/lib/services';
import type { RagQueryResponse, RagSearchMode } from '@/lib/types';
import { cn, formatDuration } from '@/lib/utils';

/** Coerce a numeric text field, falling back to a default for blank/invalid input. */
function numOr(value: string, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

export function QueryTab({ kbId }: { kbId: string }) {
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState('5');
  const [advOpen, setAdvOpen] = useState(false);
  const [minScore, setMinScore] = useState('0');
  const [searchMode, setSearchMode] = useState<RagSearchMode>('dense');
  const [rerank, setRerank] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<RagQueryResponse | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const resp = await queryKnowledgeBase(kbId, {
        query: query.trim(),
        top_k: numOr(topK, 5),
        min_score: numOr(minScore, 0),
        search_mode: searchMode,
        rerank,
      });
      setResult(resp);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader title="Retrieval Query" description="Run a retrieval query against this knowledge base." />
      <CardBody>
        <form onSubmit={run} className="flex flex-col gap-4">
          <Textarea
            label="Query"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ask a question or describe what to retrieve…"
            required
          />
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Input
              label="Top-K"
              type="number"
              min={1}
              max={50}
              value={topK}
              onChange={(e) => setTopK(e.target.value)}
            />
          </div>

          <div className="rounded-md border border-border">
            <button
              type="button"
              onClick={() => setAdvOpen((v) => !v)}
              aria-expanded={advOpen}
              className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium text-fg"
            >
              Advanced
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={cn('text-muted transition-transform', advOpen && 'rotate-180')}
              >
                <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            {advOpen && (
              <div className="grid grid-cols-1 gap-3 border-t border-border px-3 py-3 sm:grid-cols-3">
                <Input
                  label="Min Score"
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={minScore}
                  onChange={(e) => setMinScore(e.target.value)}
                />
                <Select
                  label="Search Mode"
                  value={searchMode}
                  onChange={(e) => setSearchMode(e.target.value as RagSearchMode)}
                >
                  <option value="dense">Dense</option>
                  <option value="hybrid">Hybrid</option>
                  <option value="sparse">Sparse</option>
                </Select>
                <div className="flex items-end">
                  <Switch checked={rerank} onChange={setRerank} label="Rerank" hint="Re-rank hits server-side." />
                </div>
              </div>
            )}
          </div>

          <div>
            <Button type="submit" size="md" loading={busy} disabled={!query.trim()}>
              Run Query
            </Button>
          </div>
          {error ? <ErrorBanner error={error} /> : null}
        </form>

        {result && (
          <div className="mt-5">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">
                {result.results.length} Result{result.results.length === 1 ? '' : 's'}
              </p>
              <span className="font-mono text-xs text-muted">{formatDuration(result.duration_ms)}</span>
            </div>
            {result.results.length === 0 ? (
              <p className="text-sm text-muted">No chunks matched. Try a broader query or a lower minimum score.</p>
            ) : (
              <ul className="flex flex-col gap-3">
                {result.results.map((h) => (
                  <li key={h.chunk_id} className="rounded-md border border-border bg-surface-2 px-4 py-3">
                    <div className="mb-1.5 flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-fg">{h.source?.name ?? '—'}</span>
                      <div className="flex shrink-0 items-center gap-2">
                        <CopyButton value={h.chunk_id} label="Copy Chunk ID" />
                        <Badge tone="info">Score {typeof h.score === 'number' ? h.score.toFixed(3) : '—'}</Badge>
                      </div>
                    </div>
                    <p className="whitespace-pre-wrap text-sm text-fg">{h.content}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}
