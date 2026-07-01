'use client';

import { useState } from 'react';
import Link from 'next/link';
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
  Modal,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { createSubAgent, deactivateSubAgent, listSubAgents, type SubAgent } from '@/lib/services';
import { useSession } from '@/components/SessionProvider';

export default function OrchestratorPage() {
  const toast = useToast();
  const { session } = useSession();
  const scopes = session?.scopes ?? [];
  const isOrchestrator = scopes.includes('orchestrator:manage');
  const { data, loading, error, reload } = useAsync((signal) => listSubAgents({}, signal), []);
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  async function deactivate(a: SubAgent) {
    setBusy(a.agent_id);
    try {
      await deactivateSubAgent(a.agent_id);
      toast.success('Sub-agent deactivated.');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Deactivate failed.');
    } finally {
      setBusy(null);
    }
  }

  const columns: Array<Column<SubAgent>> = [
    { key: 'name', header: 'Name', render: (a) => <span className="font-medium text-fg">{a.name}</span> },
    { key: 'type', header: 'Type', render: (a) => <Badge>{a.agent_type}</Badge> },
    { key: 'status', header: 'Status', render: (a) => <StatusBadge status={a.status} /> },
    {
      key: 'scopes',
      header: 'Scopes',
      render: (a) => (
        <div className="flex flex-wrap gap-1">
          {a.allowed_scopes.slice(0, 6).map((s) => (
            <Badge key={s}>{s}</Badge>
          ))}
          {a.allowed_scopes.length > 6 ? <span className="text-xs text-muted">+{a.allowed_scopes.length - 6}</span> : null}
        </div>
      ),
    },
    {
      key: 'actions',
      header: '',
      render: (a) =>
        a.status === 'active' ? (
          <Button size="sm" variant="danger" loading={busy === a.agent_id} onClick={() => deactivate(a)}>
            Deactivate
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Orchestrator"
        description="Your tenant's orchestrator is the only agent that can create sub-agents. Sub-agents inherit a subset of the orchestrator's scopes."
        actions={
          <Link href="/hil" className="text-sm text-brand hover:underline">
            HIL settings →
          </Link>
        }
      />
      {!isOrchestrator && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          You are signed in as a non-orchestrator agent. Sub-agent management requires the orchestrator session.
        </div>
      )}
      <Card>
        <CardHeader
          title="Sub-agents"
          description="Agents created by this orchestrator."
          actions={
            <Button size="sm" onClick={() => setCreateOpen(true)} disabled={!isOrchestrator}>
              New sub-agent
            </Button>
          }
        />
        <CardBody className="px-0 py-0">
          {error ? (
            <div className="p-4">
              <ErrorBanner error={error} title="Could not load sub-agents" />
            </div>
          ) : loading ? (
            <Loading label="Loading sub-agents…" />
          ) : (
            <Table columns={columns} rows={data?.items ?? []} rowKey={(a) => a.agent_id} empty="No sub-agents yet." />
          )}
        </CardBody>
      </Card>

      <CreateSubAgentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        orchestratorScopes={scopes}
        onCreated={() => {
          setCreateOpen(false);
          reload();
        }}
      />
    </div>
  );
}

function CreateSubAgentModal({
  open,
  onClose,
  orchestratorScopes,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  orchestratorScopes: readonly string[];
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Sub-agents may only hold a SUBSET of the orchestrator's own scopes.
  const selectable = orchestratorScopes.filter((s) => s !== 'orchestrator:manage');

  function toggle(s: string) {
    setSelected((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createSubAgent({ name: name.trim(), allowed_scopes: selected });
      onCreated();
      setName('');
      setSelected([]);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Create sub-agent"
      description="Scopes are limited to a subset of the orchestrator's own scopes."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="create-subagent-form" type="submit" loading={busy} disabled={!name.trim() || !selected.length}>
            Create
          </Button>
        </>
      }
    >
      <form id="create-subagent-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required />
        <div>
          <div className="mb-2 text-sm text-muted">Allowed scopes (subset of orchestrator):</div>
          <div className="flex flex-wrap gap-2">
            {selectable.map((s) => (
              <Button
                key={s}
                type="button"
                size="sm"
                variant={selected.includes(s) ? 'primary' : 'secondary'}
                onClick={() => toggle(s)}
              >
                {s}
              </Button>
            ))}
          </div>
        </div>
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}
