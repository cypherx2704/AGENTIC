'use client';

import { useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyButton,
  ErrorBanner,
  Input,
  Loading,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { AgentName } from '@/components/AgentNames';
import { useAsync } from '@/lib/useAsync';
import { auditExportUrl, listAuditLog, verifyAuditChain } from '@/lib/services';
import type { AuditRow, AuditVerifyResult } from '@/lib/types';
import { formatTime, shortId } from '@/lib/utils';

export default function AuditPage() {
  const toast = useToast();
  const [eventType, setEventType] = useState('');
  const [agentId, setAgentId] = useState('');
  const auditQ = useAsync(
    (signal) => listAuditLog({ event_type: eventType.trim() || undefined, agent_id: agentId.trim() || undefined, limit: 50 }, signal),
    [eventType, agentId],
  );

  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] = useState<AuditVerifyResult | null>(null);

  async function onVerify() {
    setVerifying(true);
    setVerifyResult(null);
    try {
      const result = await verifyAuditChain({});
      setVerifyResult(result);
      if (result.ok) toast.success(`Chain intact — ${result.rows_verified ?? 0} rows verified.`);
      else toast.error(`Chain BROKEN at row ${result.broken_at_row_id}.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Verify failed.');
    } finally {
      setVerifying(false);
    }
  }

  const rows = auditQ.data?.items ?? [];

  const columns: Array<Column<AuditRow>> = [
    { key: 'time', header: 'Time', render: (r) => <span className="text-xs text-muted">{formatTime(r.created_at)}</span> },
    { key: 'event', header: 'Event', render: (r) => <Badge>{r.event_type}</Badge> },
    { key: 'action', header: 'Action', render: (r) => r.action ?? '—' },
    { key: 'resource', header: 'Resource', render: (r) => <span className="font-mono text-xs">{r.resource ?? '—'}</span> },
    { key: 'decision', header: 'Decision', render: (r) => (r.decision ? <StatusBadge status={r.decision} /> : '—') },
    // agent_id is a UUID — render the agent's NAME instead (falls back to a copy affordance).
    { key: 'agent', header: 'Agent', render: (r) => <AgentName agentId={r.agent_id} className="text-sm text-fg" /> },
    // trace_id is the sanctioned-visible correlation id — keep it shown, in mono.
    {
      key: 'trace',
      header: 'Trace',
      render: (r) =>
        r.trace_id ? <span className="whitespace-nowrap font-mono text-xs text-muted">{r.trace_id}</span> : <span className="text-muted">—</span>,
    },
    // request_id is a UUID for reference — expose it via copy, never printed.
    {
      key: 'request',
      header: 'Request',
      render: (r) => (r.request_id ? <CopyButton value={r.request_id} label="Copy Request ID" /> : <span className="text-muted">—</span>),
    },
    { key: 'hash', header: 'Row Hash', render: (r) => <span className="font-mono text-xs text-muted">{shortId(r.row_hash, 12)}</span> },
  ];

  return (
    <Page>
      <PageHeader
        title="Audit Log"
        description="Tamper-evident, hash-chained audit entries for this tenant."
        actions={
          <>
            <ExportButton />
            <Button size="md" onClick={onVerify} loading={verifying}>
              Verify Chain
            </Button>
          </>
        }
      />

      <PageBody fill>
        {verifyResult && (
          <div
            className={`mb-3 shrink-0 rounded-md border px-4 py-3 text-sm ${
              verifyResult.ok ? 'border-success/40 bg-success/10' : 'border-danger/40 bg-danger/10'
            }`}
          >
            {verifyResult.ok ? (
              <p className="text-fg">
                <span className="font-semibold text-success">Chain intact.</span> {verifyResult.rows_verified ?? 0} rows verified.
                {verifyResult.to_hash && <span className="ml-2 font-mono text-xs text-muted">head: {shortId(verifyResult.to_hash, 16)}</span>}
              </p>
            ) : (
              <p className="text-fg">
                <span className="font-semibold text-danger">Chain BROKEN</span> at row {verifyResult.broken_at_row_id}.
                <span className="ml-2 font-mono text-xs text-muted">
                  expected {shortId(verifyResult.expected_prev_hash, 12)} · actual {shortId(verifyResult.actual_prev_hash, 12)}
                </span>
              </p>
            )}
          </div>
        )}

        <Card className="mb-3 shrink-0">
          <CardBody>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Input label="Event Type" placeholder="e.g. agent.created" value={eventType} onChange={(e) => setEventType(e.target.value)} />
              <Input label="Agent ID" placeholder="filter by agent" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
            </div>
          </CardBody>
        </Card>

        <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <CardHeader title="Entries" />
          <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
            {auditQ.error ? (
              <div className="p-4">
                <ErrorBanner error={auditQ.error} title="Could not load the audit log" />
              </div>
            ) : auditQ.loading ? (
              <Loading label="Loading audit log…" />
            ) : (
              <Table columns={columns} rows={rows} rowKey={(r) => String(r.id)} empty="No audit entries match these filters." />
            )}
          </CardBody>
        </Card>
      </PageBody>
    </Page>
  );
}

/**
 * Audit-log export — a same-origin, cookie-authenticated GET rendered as a download link
 * (NOT the JSON api() path, since the response is a file). This page has no from/to filters,
 * so it exports the full accessible range. Styled to match a secondary Button.
 */
function ExportButton() {
  return (
    <a
      href={auditExportUrl()}
      download
      className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-border-2 bg-surface px-3 text-sm font-medium text-fg transition-colors hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      Export
    </a>
  );
}
