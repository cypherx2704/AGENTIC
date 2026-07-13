'use client';

import { useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentName } from '@/components/AgentNames';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  Select,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import {
  assignPolicy,
  deleteCustomRule,
  listAgents,
  listCustomRules,
  listPolicies,
  listViolations,
  rotateRedactionKey,
} from '@/lib/services';
import type { CustomRule, Policy, Violation } from '@/lib/types';
import { cn, formatTime } from '@/lib/utils';
import { PolicyEditor } from './PolicyEditor';
import { CustomRuleEditor } from './CustomRuleEditor';
import { CheckPlayground } from './CheckPlayground';
import { SimulatePanel, type SimulateTarget } from './SimulatePanel';

type TabKey = 'policies' | 'rules' | 'playground' | 'violations';

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'policies', label: 'Policies' },
  { key: 'rules', label: 'Custom Rules' },
  { key: 'playground', label: 'Test Playground' },
  { key: 'violations', label: 'Violations' },
];

export default function GuardrailsPage() {
  const [tab, setTab] = useState<TabKey>('policies');

  return (
    <Page>
      <PageHeader
        title="Guardrails"
        description="Author policies and custom rules, test them live, and review the violation log."
      />
      <PageBody>
        <div className="mb-4 flex items-center gap-1 border-b border-border">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              aria-current={tab === t.key ? 'page' : undefined}
              className={cn(
                'relative -mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                tab === t.key ? 'border-brand text-fg-strong' : 'border-transparent text-muted hover:text-fg',
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === 'policies' ? <PoliciesTab /> : null}
        {tab === 'rules' ? <CustomRulesTab /> : null}
        {tab === 'playground' ? <CheckPlayground /> : null}
        {tab === 'violations' ? <ViolationsLog /> : null}
      </PageBody>
    </Page>
  );
}

// ── Policies tab ────────────────────────────────────────────────────────────────────
function PoliciesTab() {
  const toast = useToast();
  const policiesQ = useAsync((signal) => listPolicies(signal), []);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<Policy | null>(null);
  const [assignFor, setAssignFor] = useState<Policy | null>(null);
  const [simTarget, setSimTarget] = useState<SimulateTarget | null>(null);
  const [rotateOpen, setRotateOpen] = useState(false);
  const [rotating, setRotating] = useState(false);

  const policies = policiesQ.data?.policies ?? [];

  async function rotate() {
    setRotating(true);
    try {
      await rotateRedactionKey();
      toast.success('Redaction key rotated. The previous key stays valid during the grace window.');
      setRotateOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not rotate the redaction key.');
    } finally {
      setRotating(false);
    }
  }

  const policyColumns: Array<Column<Policy>> = [
    { key: 'name', header: 'Name', render: (p) => <span className="font-medium text-fg">{p.name}</span> },
    {
      key: 'id',
      header: 'Policy ID',
      render: (p) => <CopyButton value={p.policy_id} label="Copy Policy ID" />,
    },
    {
      key: 'default',
      header: 'Scope',
      render: (p) => (p.is_default ? <Badge tone="info">Platform Default</Badge> : <Badge>Tenant</Badge>),
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
      className: 'text-right',
      render: (p) => (
        <div className="flex justify-end gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setSimTarget({ kind: 'stored', policyId: p.policy_id, policyName: p.name })}
          >
            Simulate
          </Button>
          {p.is_default ? (
            <span className="self-center text-xs text-muted">Read-only</span>
          ) : (
            <>
              <Button size="sm" variant="secondary" onClick={() => setAssignFor(p)}>
                Assign
              </Button>
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
            </>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardHeader
          title="Policies"
          description="Applied on the PRE and POST guardrail stages of every task."
          actions={
            <>
              <Button size="sm" variant="secondary" onClick={() => setRotateOpen(true)}>
                Rotate Redaction Key
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setEditing(null);
                  setEditorOpen(true);
                }}
              >
                New Policy
              </Button>
            </>
          }
        />
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

      {editorOpen ? (
        <PolicyEditor
          open
          policy={editing}
          onClose={() => setEditorOpen(false)}
          onSaved={() => {
            setEditorOpen(false);
            policiesQ.reload();
          }}
        />
      ) : null}

      {assignFor ? (
        <AssignModal policy={assignFor} onClose={() => setAssignFor(null)} />
      ) : null}

      {simTarget ? <SimulatePanel target={simTarget} onClose={() => setSimTarget(null)} /> : null}

      <ConfirmDialog
        open={rotateOpen}
        onClose={() => setRotateOpen(false)}
        onConfirm={rotate}
        loading={rotating}
        title="Rotate Redaction Key"
        description="Mint a new tenant redaction HMAC key."
        confirmLabel="Rotate Key"
        confirmVariant="primary"
      >
        <p className="text-sm text-muted">
          The previous key remains valid during the grace window so tokens minted just before rotation still resolve.
          This is a tenant:admin maintenance action.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/** Small modal: repoint a single agent at the selected policy. */
function AssignModal({ policy, onClose }: { policy: Policy; onClose: () => void }) {
  const toast = useToast();
  const agentsQ = useAsync((signal) => listAgents({ limit: 100 }, signal), []);
  const [agentId, setAgentId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const agents = agentsQ.data ? (agentsQ.data.items ?? agentsQ.data.agents ?? agentsQ.data.data ?? []) : [];

  async function assign() {
    if (!agentId) return;
    setBusy(true);
    setError(null);
    try {
      await assignPolicy(policy.policy_id, agentId);
      toast.success('Policy assigned.');
      onClose();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      size="sm"
      title="Assign Policy"
      description={`Repoint an agent at "${policy.name}".`}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={assign} loading={busy} disabled={!agentId}>
            Assign
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {agentsQ.loading ? (
          <Loading label="Loading agents…" />
        ) : agentsQ.error ? (
          <ErrorBanner error={agentsQ.error} title="Could not load agents" />
        ) : (
          <Select label="Agent" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">Select an agent…</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.name}
              </option>
            ))}
          </Select>
        )}
        {error ? <ErrorBanner error={error} /> : null}
      </div>
    </Modal>
  );
}

// ── Custom Rules tab ──────────────────────────────────────────────────────────────────
function CustomRulesTab() {
  const toast = useToast();
  const rulesQ = useAsync((signal) => listCustomRules(signal), []);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<CustomRule | null>(null);
  const [deleteFor, setDeleteFor] = useState<CustomRule | null>(null);
  const [deleting, setDeleting] = useState(false);

  const rules = rulesQ.data ?? [];

  async function doDelete() {
    if (!deleteFor) return;
    setDeleting(true);
    try {
      await deleteCustomRule(deleteFor.id);
      toast.success('Custom rule deleted.');
      setDeleteFor(null);
      rulesQ.reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not delete the rule.');
    } finally {
      setDeleting(false);
    }
  }

  const columns: Array<Column<CustomRule>> = [
    { key: 'name', header: 'Name', render: (r) => <span className="font-medium text-fg">{r.name}</span> },
    {
      key: 'type',
      header: 'Type',
      render: (r) => <Badge>{r.type === 'classifier-threshold' ? 'Classifier' : 'Regex'}</Badge>,
    },
    {
      key: 'direction',
      header: 'Direction',
      render: (r) => <span className="text-xs uppercase text-muted">{r.direction}</span>,
    },
    { key: 'category', header: 'Category', render: (r) => <span className="text-sm text-fg">{r.category}</span> },
    { key: 'severity', header: 'Severity', render: (r) => <StatusBadge status={r.severity} /> },
    {
      key: 'action',
      header: 'Action',
      render: (r) => <span className="text-sm capitalize text-fg">{r.default_action}</span>,
    },
    { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (r) => (
        <div className="flex justify-end gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setEditing(r);
              setEditorOpen(true);
            }}
          >
            Edit
          </Button>
          <Button size="sm" variant="danger" onClick={() => setDeleteFor(r)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Custom Rules"
        description="Tenant-authored regex and classifier-threshold rules, overlaid on the effective policy."
        actions={
          <Button
            size="sm"
            onClick={() => {
              setEditing(null);
              setEditorOpen(true);
            }}
          >
            New Custom Rule
          </Button>
        }
      />
      <CardBody className="px-0 py-0">
        {rulesQ.error ? (
          <div className="p-4">
            <ErrorBanner error={rulesQ.error} title="Could not load custom rules" />
          </div>
        ) : rulesQ.loading ? (
          <Loading label="Loading custom rules…" />
        ) : (
          <Table columns={columns} rows={rules} rowKey={(r) => r.id} empty="No custom rules yet." />
        )}
      </CardBody>

      {editorOpen ? (
        <CustomRuleEditor
          open
          rule={editing}
          onClose={() => setEditorOpen(false)}
          onSaved={() => {
            setEditorOpen(false);
            rulesQ.reload();
          }}
        />
      ) : null}

      <ConfirmDialog
        open={deleteFor !== null}
        onClose={() => setDeleteFor(null)}
        onConfirm={doDelete}
        loading={deleting}
        title="Delete Custom Rule"
        description={deleteFor ? `Retire "${deleteFor.name}"? The version history is kept for audit.` : undefined}
        confirmLabel="Delete"
      />
    </Card>
  );
}

// ── Violations tab (preserved verbatim) ───────────────────────────────────────────────
const DECISIONS = ['', 'allow', 'warn', 'redact', 'block'];
const DECISION_LABEL: Record<string, string> = {
  '': 'All',
  allow: 'Allow',
  warn: 'Warn',
  redact: 'Redact',
  block: 'Block',
};

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
    { key: 'direction', header: 'Direction', render: (v) => <Badge>{v.direction}</Badge> },
    { key: 'decision', header: 'Decision', render: (v) => <StatusBadge status={v.decision} /> },
    { key: 'rule', header: 'Rule', render: (v) => <span className="font-mono text-xs text-fg">{v.rule_name ?? v.rule_id ?? '—'}</span> },
    { key: 'severity', header: 'Severity', render: (v) => v.severity ?? '—' },
    { key: 'category', header: 'Category', render: (v) => v.category ?? '—' },
    {
      key: 'matched',
      header: 'Matched (Safe)',
      render: (v) => <span className="font-mono text-xs text-muted">{v.matched ?? '—'}</span>,
    },
    { key: 'agent', header: 'Agent', render: (v) => <AgentName agentId={v.agent_id} /> },
  ];

  return (
    <Card>
      <CardHeader
        title="Violation Log"
        description="Redaction-safe — matched values are tokens/truncations, never raw PII."
      />
      <CardBody className="px-0 py-0">
        <div className="grid grid-cols-1 gap-3 px-4 py-3 sm:grid-cols-2">
          <Select label="Decision" value={decision} onChange={(e) => setDecision(e.target.value)}>
            {DECISIONS.map((d) => (
              <option key={d} value={d}>
                {DECISION_LABEL[d]}
              </option>
            ))}
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
