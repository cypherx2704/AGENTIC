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
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { listAuditLog, verifyAuditChain } from '@/lib/services';
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
    { key: 'agent', header: 'Agent', render: (r) => <span className="font-mono text-xs text-muted">{shortId(r.agent_id, 8)}</span> },
    { key: 'hash', header: 'Row hash', render: (r) => <span className="font-mono text-xs text-muted">{shortId(r.row_hash, 12)}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Audit log"
        description="Tamper-evident, hash-chained audit entries for this tenant."
        actions={
          <Button size="sm" onClick={onVerify} loading={verifying}>
            Verify chain
          </Button>
        }
      />

      {verifyResult && (
        <div
          className={`mb-4 rounded-md border px-4 py-3 text-sm ${
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

      <Card className="mb-4">
        <CardBody>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Input label="Event type" placeholder="e.g. agent.created" value={eventType} onChange={(e) => setEventType(e.target.value)} />
            <Input label="Agent ID" placeholder="filter by agent" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Entries" />
        <CardBody className="px-0 py-0">
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
    </div>
  );
}
