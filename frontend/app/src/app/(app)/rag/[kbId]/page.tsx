'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  ErrorBanner,
  Loading,
  Stat,
  StatusBadge,
  useToast,
} from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { deleteKnowledgeBase, getKbStatus, getKnowledgeBase } from '@/lib/services';
import type { KbDetail, KbStatus } from '@/lib/types';
import { cn, formatNumber, formatTime } from '@/lib/utils';
import { AclTab } from './AclTab';
import { DocumentsTab } from './DocumentsTab';
import { QueryTab } from './QueryTab';

interface KbView {
  kb: KbDetail;
  status: KbStatus | null;
}

type TabKey = 'overview' | 'documents' | 'query' | 'access';
const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'documents', label: 'Documents' },
  { key: 'query', label: 'Query' },
  { key: 'access', label: 'Access' },
];

export default function KbDetailPage() {
  const { kbId } = useParams<{ kbId: string }>();
  const router = useRouter();
  const toast = useToast();

  const { data, loading, error } = useAsync<KbView>(async (signal) => {
    // The status rollup is best-effort — a missing status must not blank the whole page.
    const [kbR, statusR] = await Promise.allSettled([
      getKnowledgeBase(kbId, signal),
      getKbStatus(kbId, signal),
    ]);
    if (kbR.status === 'rejected') throw kbR.reason;
    return { kb: kbR.value, status: statusR.status === 'fulfilled' ? statusR.value : null };
  }, [kbId]);

  const [tab, setTab] = useState<TabKey>('overview');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const kb = data?.kb ?? null;

  async function onDelete() {
    if (!kb) return;
    setDeleting(true);
    try {
      await deleteKnowledgeBase(kb.kb_id);
      toast.success(`Knowledge base '${kb.name}' deleted.`);
      router.push('/rag');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed.');
      setDeleting(false);
      setConfirmDelete(false);
    }
  }

  return (
    <Page>
      <PageHeader
        title={kb?.name ?? 'Knowledge Base'}
        description={<CopyButton value={kbId} label="Copy KB ID" />}
        actions={
          <>
            {kb && <StatusBadge status={kb.status} />}
            <Link href="/rag" className="text-[13px] font-medium text-brand hover:underline">
              ← Knowledge Bases
            </Link>
            {kb && (
              <Button variant="danger" size="md" onClick={() => setConfirmDelete(true)}>
                Delete KB
              </Button>
            )}
          </>
        }
      />

      <PageBody>
      {error ? (
        <ErrorBanner error={error} title="Could not load this knowledge base" />
      ) : loading ? (
        <Loading label="Loading knowledge base…" />
      ) : data && kb ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-1 border-b border-border">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                aria-current={tab === t.key ? 'page' : undefined}
                className={cn(
                  'relative -mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                  tab === t.key
                    ? 'border-brand text-fg-strong'
                    : 'border-transparent text-muted hover:text-fg',
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          {tab === 'overview' && <OverviewTab kb={kb} status={data.status} />}
          {tab === 'documents' && <DocumentsTab kbId={kb.kb_id} />}
          {tab === 'query' && <QueryTab kbId={kb.kb_id} />}
          {tab === 'access' && <AclTab kbId={kb.kb_id} />}
        </div>
      ) : null}

      <ConfirmDialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        onConfirm={onDelete}
        title="Delete This Knowledge Base?"
        description="This permanently removes the KB, its documents, and all embeddings."
        confirmLabel="Delete Knowledge Base"
        loading={deleting}
        confirmPhrase={kb?.name}
      >
        {kb && (
          <p className="text-sm text-muted">
            Deleting <span className="font-medium text-fg">{kb.name}</span> cannot be undone. Every document and chunk in
            this knowledge base will be lost.
          </p>
        )}
      </ConfirmDialog>
      </PageBody>
    </Page>
  );
}

function OverviewTab({ kb, status }: { kb: KbDetail; status: KbStatus | null }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Documents" value={formatNumber(status?.document_count ?? null)} />
        <Stat label="Chunks" value={formatNumber(status?.chunk_count ?? null)} />
        <Stat label="Pending" value={formatNumber(status?.pending_docs ?? null)} />
        <Stat label="Failed" value={formatNumber(status?.failed_docs ?? null)} />
      </div>

      <Card>
        <CardHeader title="Configuration" description="Chunking and embedding settings for this knowledge base." />
        <CardBody>
          <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
            <Field label="Status" value={<StatusBadge status={kb.status} />} />
            <Field label="Chunking Strategy" value={<span className="capitalize">{kb.chunking_strategy}</span>} />
            <Field
              label="Chunk Size / Overlap"
              value={
                <span className="font-mono tabular-nums">
                  {kb.chunk_size} / {kb.chunk_overlap}
                </span>
              }
            />
            <Field label="Embedding Alias" value={<span className="font-mono text-xs">{kb.embedding_model_alias}</span>} />
            <Field
              label="Embedding Model"
              value={<span className="font-mono text-xs">{kb.embedding_model_resolved || '—'}</span>}
            />
            <Field label="Embedding Dim" value={<span className="font-mono tabular-nums">{formatNumber(kb.embedding_dim)}</span>} />
            <Field label="Created" value={formatTime(kb.created_at)} />
            <Field label="Updated" value={formatTime(kb.updated_at)} />
            {status?.last_updated_at ? <Field label="Last Ingest" value={formatTime(status.last_updated_at)} /> : null}
          </dl>

          {kb.description ? (
            <div className="mt-4 rounded-md border border-border bg-surface-2 px-4 py-3">
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Description</p>
              <p className="whitespace-pre-wrap text-sm text-fg">{kb.description}</p>
            </div>
          ) : null}
        </CardBody>
      </Card>
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</p>
      <p className="mt-1 text-fg">{value}</p>
    </div>
  );
}
