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
  ErrorBanner,
  Loading,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import {
  denyHilApproval,
  getHilConfig,
  grantHilApproval,
  listHilApprovals,
  putHilConfig,
  type HilApproval,
} from '@/lib/services';
import { formatTime } from '@/lib/utils';

const MODES = ['automated', 'human_in_loop', 'partial'] as const;
const MODE_LABEL: Record<string, string> = {
  automated: 'Automated',
  human_in_loop: 'Human-in-Loop',
  partial: 'Partial',
};
const TRIGGERS = ['tool_execution', 'sub_agent_creation', 'llm_restriction', 'skill_execution'];
const TRIGGER_LABEL: Record<string, string> = {
  tool_execution: 'Tool Execution',
  sub_agent_creation: 'Sub-agent Creation',
  llm_restriction: 'LLM Restriction',
  skill_execution: 'Skill Execution',
};

export default function HilPage() {
  return (
    <Page>
      <PageHeader
        title="Human-in-the-Loop Approvals"
        description="Review and resolve pending agent-action approvals, and set the orchestrator's HIL mode."
      />
      <PageBody fill className="gap-3">
        <HilConfigCard />
        <ApprovalsCard />
      </PageBody>
    </Page>
  );
}

function HilConfigCard() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => getHilConfig(signal), []);
  const [mode, setMode] = useState<string>('');
  const [triggers, setTriggers] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  // Initialise local state from the loaded config (once).
  const effectiveMode = mode || data?.default_mode || 'automated';
  const effectiveTriggers = triggers.length || mode ? triggers : (data?.ask_on_triggers ?? []);

  async function save() {
    setSaving(true);
    try {
      await putHilConfig({ default_mode: effectiveMode, ask_on_triggers: effectiveTriggers });
      toast.success('HIL configuration saved.');
      setMode('');
      setTriggers([]);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Save failed.');
    } finally {
      setSaving(false);
    }
  }

  function toggleTrigger(t: string) {
    const base = triggers.length || mode ? triggers : (data?.ask_on_triggers ?? []);
    setMode(effectiveMode); // lock in current mode so edits persist
    setTriggers(base.includes(t) ? base.filter((x) => x !== t) : [...base, t]);
  }

  return (
    <Card className="shrink-0">
      <CardHeader
        title="Orchestrator HIL Mode"
        description="Automated = never pause · Human-in-Loop = always ask · Partial = ask only on selected triggers."
      />
      <CardBody>
        {error ? (
          <ErrorBanner error={error} title="Could not load HIL config" />
        ) : loading ? (
          <Loading label="Loading…" />
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-2">
              {MODES.map((m) => (
                <Button
                  key={m}
                  size="md"
                  variant={effectiveMode === m ? 'primary' : 'secondary'}
                  onClick={() => {
                    setMode(m);
                    if (!triggers.length) setTriggers(data?.ask_on_triggers ?? []);
                  }}
                >
                  {MODE_LABEL[m] ?? m}
                </Button>
              ))}
            </div>
            {effectiveMode === 'partial' && (
              <div>
                <div className="mb-2 text-sm text-muted">Pause for approval on:</div>
                <div className="flex flex-wrap gap-2">
                  {TRIGGERS.map((t) => (
                    <Button
                      key={t}
                      size="md"
                      variant={effectiveTriggers.includes(t) ? 'primary' : 'secondary'}
                      onClick={() => toggleTrigger(t)}
                    >
                      {TRIGGER_LABEL[t] ?? t}
                    </Button>
                  ))}
                </div>
              </div>
            )}
            <div>
              <Button onClick={save} loading={saving}>
                Save HIL Mode
              </Button>
            </div>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function ApprovalsCard() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listHilApprovals({}, signal), []);
  const [busy, setBusy] = useState<string | null>(null);

  async function resolve(req: HilApproval, decision: 'grant' | 'deny') {
    setBusy(req.request_id);
    try {
      if (decision === 'grant') await grantHilApproval(req.request_id);
      else await denyHilApproval(req.request_id);
      toast.success(`Request ${decision === 'grant' ? 'granted' : 'denied'}.`);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setBusy(null);
    }
  }

  const columns: Array<Column<HilApproval>> = [
    { key: 'op', header: 'Operation', render: (r) => <Badge>{r.operation_type ?? '—'}</Badge> },
    {
      key: 'ctx',
      header: 'Context',
      render: (r) => (
        <span className="font-mono text-xs text-muted">
          {Object.entries(r.context || {})
            .map(([k, v]) => `${k}=${String(v)}`)
            .join(', ') || '—'}
        </span>
      ),
    },
    { key: 'agent', header: 'Agent', render: (r) => <AgentName agentId={r.agent_id} /> },
    { key: 'requested', header: 'Requested', render: (r) => <span className="text-xs text-muted">{formatTime(r.requested_at)}</span> },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (r) => (
        <div className="flex justify-end gap-2">
          <Button size="sm" loading={busy === r.request_id} onClick={() => resolve(r, 'grant')}>
            Grant
          </Button>
          <Button size="sm" variant="danger" loading={busy === r.request_id} onClick={() => resolve(r, 'deny')}>
            Deny
          </Button>
        </div>
      ),
    },
  ];

  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <CardHeader
        title="Pending Approvals"
        description="Agents waiting on a human decision before performing an ask-mode action."
        actions={
          <Button size="md" variant="secondary" onClick={reload}>
            Refresh
          </Button>
        }
      />
      <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
        {error ? (
          <div className="p-4">
            <ErrorBanner error={error} title="Could not load approvals" />
          </div>
        ) : loading ? (
          <Loading label="Loading approvals…" />
        ) : (
          <Table
            columns={columns}
            rows={data?.items ?? []}
            rowKey={(r) => r.request_id}
            empty="No pending approvals."
          />
        )}
      </CardBody>
    </Card>
  );
}
