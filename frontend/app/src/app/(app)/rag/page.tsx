'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyButton,
  ErrorBanner,
  Loading,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { getKbStatus, listKnowledgeBases } from '@/lib/services';
import type { KbStatus, KnowledgeBase } from '@/lib/types';
import { formatNumber, formatTime } from '@/lib/utils';
import { CreateKbModal } from './CreateKbModal';

/** A KB row joined with its rollup counts. Status is null when the per-KB status call failed. */
interface EnrichedKb {
  kb: KnowledgeBase;
  status: KbStatus | null;
}

/**
 * The KB list returns rows without counts, so enrich each row with a getKbStatus call.
 * allSettled keeps one failing status call from blanking the whole table — that row just
 * degrades to "—" for its counts.
 */
async function loadKbs(signal: AbortSignal): Promise<EnrichedKb[]> {
  const kbs = await listKnowledgeBases(signal);
  const settled = await Promise.allSettled(kbs.map((k) => getKbStatus(k.kb_id, signal)));
  return kbs.map((kb, i) => {
    const s = settled[i];
    return { kb, status: s.status === 'fulfilled' ? s.value : null };
  });
}

/** KnowledgeBase carries an index signature, so coerce the (untyped) resolved-model field. */
function asString(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

export default function RagPage() {
  const router = useRouter();
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(loadKbs, []);
  const [createOpen, setCreateOpen] = useState(false);

  const rows = data ?? [];

  const columns: Array<Column<EnrichedKb>> = [
    {
      key: 'name',
      header: 'Name',
      render: ({ kb }) => (
        <Link href={`/rag/${kb.kb_id}`} className="font-medium text-fg hover:text-brand hover:underline">
          {kb.name}
        </Link>
      ),
    },
    {
      key: 'kb_id',
      header: 'KB ID',
      render: ({ kb }) => <CopyButton value={kb.kb_id} label="Copy KB ID" />,
    },
    { key: 'status', header: 'Status', render: ({ kb }) => <StatusBadge status={kb.status} /> },
    {
      key: 'model',
      header: 'Embedding Model',
      render: ({ kb }) => (
        <span className="font-mono text-xs text-muted">
          {asString(kb.embedding_model_resolved) ?? kb.embedding_model ?? '—'}
        </span>
      ),
    },
    {
      key: 'docs',
      header: 'Docs',
      className: 'text-right',
      render: ({ status }) => (
        <span className="font-mono text-xs tabular-nums">{formatNumber(status?.document_count ?? null)}</span>
      ),
    },
    {
      key: 'chunks',
      header: 'Chunks',
      className: 'text-right',
      render: ({ status }) => (
        <span className="font-mono text-xs tabular-nums">{formatNumber(status?.chunk_count ?? null)}</span>
      ),
    },
    {
      key: 'updated',
      header: 'Updated',
      className: 'text-right',
      render: ({ kb }) => <span className="text-xs text-muted">{formatTime(kb.updated_at)}</span>,
    },
  ];

  return (
    <Page>
      <PageHeader
        title="Knowledge Bases"
        description="Create and manage knowledge bases, ingest documents, and run retrieval queries."
      />

      <PageBody fill>
      <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <CardHeader
          title="Knowledge Bases"
          actions={
            <>
              <Button variant="secondary" size="md" onClick={reload}>
                Refresh
              </Button>
              <Button size="md" onClick={() => setCreateOpen(true)}>
                New Knowledge Base
              </Button>
            </>
          }
        />
        <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
          {error ? (
            <div className="p-4">
              <ErrorBanner error={error} title="Could not load knowledge bases" />
            </div>
          ) : loading ? (
            <Loading label="Loading knowledge bases…" />
          ) : (
            <Table
              columns={columns}
              rows={rows}
              rowKey={({ kb }) => kb.kb_id}
              empty="No knowledge bases yet. Create one to get started."
            />
          )}
        </CardBody>
      </Card>

      <CreateKbModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(kb) => {
          setCreateOpen(false);
          toast.success(`Knowledge base '${kb.name}' created.`);
          router.push(`/rag/${kb.kb_id}`);
        }}
      />
      </PageBody>
    </Page>
  );
}
