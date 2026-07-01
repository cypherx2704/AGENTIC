'use client';

import { useState } from 'react';
import { PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ErrorBanner,
  Input,
  Loading,
  Select,
  StatusBadge,
  Table,
  Textarea,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { listKnowledgeBases, queryKnowledgeBase } from '@/lib/services';
import type { KbQueryResult } from '@/lib/services';
import type { KnowledgeBase } from '@/lib/types';
import { formatNumber, formatTime, shortId } from '@/lib/utils';

export default function RagPage() {
  const kbQ = useAsync((signal) => listKnowledgeBases(signal), []);
  const kbs = kbQ.data?.data ?? kbQ.data?.knowledge_bases ?? [];

  const columns: Array<Column<KnowledgeBase>> = [
    { key: 'name', header: 'Name', render: (k) => <span className="font-medium text-fg">{k.name}</span> },
    { key: 'id', header: 'KB ID', render: (k) => <span className="font-mono text-xs text-muted">{shortId(k.kb_id, 12)}</span> },
    { key: 'status', header: 'Status', render: (k) => <StatusBadge status={k.status} /> },
    { key: 'model', header: 'Embedding model', render: (k) => k.embedding_model ?? '—' },
    { key: 'docs', header: 'Docs', render: (k) => formatNumber(k.document_count ?? null) },
    { key: 'chunks', header: 'Chunks', render: (k) => formatNumber(k.chunk_count ?? null) },
    { key: 'updated', header: 'Updated', render: (k) => <span className="text-xs text-muted">{formatTime(k.updated_at)}</span> },
  ];

  return (
    <div>
      <PageHeader title="Knowledge bases" description="KB status and an ad-hoc test-query box (RAG / WP09)." />

      <Card className="mb-6">
        <CardHeader title="Knowledge bases" actions={<Button size="sm" variant="secondary" onClick={kbQ.reload}>Refresh</Button>} />
        <CardBody className="px-0 py-0">
          {kbQ.error ? (
            <div className="p-4">
              <ErrorBanner error={kbQ.error} title="Could not load knowledge bases" />
            </div>
          ) : kbQ.loading ? (
            <Loading label="Loading knowledge bases…" />
          ) : (
            <Table columns={columns} rows={kbs} rowKey={(k) => k.kb_id} empty="No knowledge bases yet." />
          )}
        </CardBody>
      </Card>

      <TestQuery kbs={kbs} />
    </div>
  );
}

function TestQuery({ kbs }: { kbs: KnowledgeBase[] }) {
  const [kbId, setKbId] = useState('');
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<KbQueryResult | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await queryKnowledgeBase(kbId.trim(), { query, top_k: Number(topK) }));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  const hits = result?.results ?? (result?.data as KbQueryResult['results']) ?? [];

  return (
    <Card>
      <CardHeader title="Test query" description="Run a retrieval query against a knowledge base." />
      <CardBody>
        <form onSubmit={run} className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {kbs.length > 0 ? (
              <Select label="Knowledge base" value={kbId} onChange={(e) => setKbId(e.target.value)} required>
                <option value="">select…</option>
                {kbs.map((k) => (
                  <option key={k.kb_id} value={k.kb_id}>
                    {k.name}
                  </option>
                ))}
              </Select>
            ) : (
              <Input label="KB ID" value={kbId} onChange={(e) => setKbId(e.target.value)} required />
            )}
            <Input label="Top-k" type="number" min={1} max={20} value={topK} onChange={(e) => setTopK(Number(e.target.value))} />
          </div>
          <Textarea label="Query" value={query} onChange={(e) => setQuery(e.target.value)} required />
          <div>
            <Button type="submit" loading={busy} disabled={!kbId.trim() || !query.trim()}>
              Run query
            </Button>
          </div>
          {error ? <ErrorBanner error={error} /> : null}
        </form>

        {result && (
          <div className="mt-5">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
              {hits.length} result{hits.length === 1 ? '' : 's'}
            </p>
            {hits.length === 0 ? (
              <p className="text-sm text-muted">No chunks matched.</p>
            ) : (
              <ul className="flex flex-col gap-3">
                {hits.map((h, i) => (
                  <li key={(h.chunk_id as string) ?? i} className="rounded-md border border-border bg-surface-2 px-4 py-3">
                    <div className="mb-1 flex items-center justify-between">
                      <span className="font-mono text-xs text-muted">{shortId((h.chunk_id as string) ?? '', 12)}</span>
                      {h.score != null && <Badge tone="info">score {Number(h.score).toFixed(3)}</Badge>}
                    </div>
                    <p className="whitespace-pre-wrap text-sm text-fg">{String(h.text ?? h.content ?? '')}</p>
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
