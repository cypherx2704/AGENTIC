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
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { listPolicies, listViolations } from '@/lib/services';
import type { Policy, Violation } from '@/lib/types';
import { formatTime, shortId } from '@/lib/utils';
import { PolicyEditor } from './PolicyEditor';

export default function GuardrailsPage() {
  const policiesQ = useAsync((signal) => listPolicies(signal), []);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<Policy | null>(null);

  const policies = policiesQ.data?.policies ?? [];

  const policyColumns: Array<Column<Policy>> = [
    { key: 'name', header: 'Name', render: (p) => <span className="font-medium text-fg">{p.name}</span> },
    {
      key: 'id',
      header: 'Policy ID',
      render: (p) => <span className="font-mono text-xs text-muted">{shortId(p.policy_id, 12)}</span>,
    },
    {
      key: 'default',
      header: 'Scope',
      render: (p) => (p.is_default ? <Badge tone="info">platform default</Badge> : <Badge>tenant</Badge>),
    },
    { key: 'status', header: 'Status', render: (p) => <StatusBadge status={p.status} /> },
    {
      key: 'rules',
      header: 'Rules',
      render: (p) => <span className="text-sm text-muted">{p.rules?.filter((r) => r.enabled).length ?? 0} enabled</span>,
    },
    {
      key: 'actions',
      header: '',
      render: (p) =>
        p.is_default ? (
          <span className="text-xs text-muted">read-only</span>
        ) : (
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setEditing(p);
              setEditorOpen(true);
            }}
          >
            Edit
          </Button>
        ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="Guardrails"
        description="Policies and the violation log. The platform default is read-only."
        actions={
          <Button
            size="sm"
            onClick={() => {
              setEditing(null);
              setEditorOpen(true);
            }}
          >
            New policy
          </Button>
        }
      />

      <Card className="mb-6">
        <CardHeader title="Policies" />
        <CardBody className="px-0 py-0">
          {policiesQ.error ? (
            <div className="p-4">
              <ErrorBanner error={policiesQ.error} title="Could not load policies" />
            </div>
          ) : policiesQ.loading ? (
            <Loading label="Loading policies…" />
          ) : (
            <Table columns={policyColumns} rows={policies} rowKey={(p) => p.policy_id} empty="No policies yet." />
          )}
        </CardBody>
      </Card>

      <ViolationsLog />

      <PolicyEditor
        open={editorOpen}
        policy={editing}
        onClose={() => setEditorOpen(false)}
        onSaved={() => {
          setEditorOpen(false);
          policiesQ.reload();
        }}
      />
    </div>
  );
}

function ViolationsLog() {
  const [decision, setDecision] = useState('');
  const [agentId, setAgentId] = useState('');
  const violationsQ = useAsync(
    (signal) => listViolations({ decision: decision || undefined, agent_id: agentId.trim() || undefined, limit: 50 }, signal),
    [decision, agentId],
  );

  const violations = violationsQ.data?.violations ?? [];

  const columns: Array<Column<Violation>> = [
    { key: 'time', header: 'Time', render: (v) => <span className="text-xs text-muted">{formatTime(v.created_at)}</span> },
    { key: 'direction', header: 'Dir', render: (v) => <Badge>{v.direction}</Badge> },
    { key: 'decision', header: 'Decision', render: (v) => <StatusBadge status={v.decision} /> },
    { key: 'rule', header: 'Rule', render: (v) => <span className="font-mono text-xs text-fg">{v.rule_name ?? v.rule_id ?? '—'}</span> },
    { key: 'severity', header: 'Severity', render: (v) => v.severity ?? '—' },
    { key: 'category', header: 'Category', render: (v) => v.category ?? '—' },
    {
      key: 'matched',
      header: 'Matched (safe)',
      render: (v) => <span className="font-mono text-xs text-muted">{v.matched ?? '—'}</span>,
    },
    { key: 'agent', header: 'Agent', render: (v) => <span className="font-mono text-xs text-muted">{shortId(v.agent_id, 8)}</span> },
  ];

  return (
    <Card>
      <CardHeader
        title="Violation log"
        description="Redaction-safe — matched values are tokens/truncations, never raw PII."
      />
      <CardBody className="px-0 py-0">
        <div className="grid grid-cols-1 gap-3 px-4 py-3 sm:grid-cols-2">
          <Select label="Decision" value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="">all</option>
            <option value="allow">allow</option>
            <option value="warn">warn</option>
            <option value="redact">redact</option>
            <option value="block">block</option>
          </Select>
          <Input label="Agent ID" placeholder="filter by agent" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
        </div>
        {violationsQ.error ? (
          <div className="p-4">
            <ErrorBanner error={violationsQ.error} title="Could not load violations" />
          </div>
        ) : violationsQ.loading ? (
          <Loading label="Loading violations…" />
        ) : (
          <Table columns={columns} rows={violations} rowKey={(v) => v.id} empty="No violations recorded." />
        )}
      </CardBody>
    </Card>
  );
}
