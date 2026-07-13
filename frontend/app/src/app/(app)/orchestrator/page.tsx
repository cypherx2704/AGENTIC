'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Callout,
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
import { ScopeSelector } from '@/components/ScopeSelector';
import { useAsync } from '@/lib/useAsync';
import { createSubAgent, deactivateSubAgent, listSubAgents, updateSubAgent, type SubAgent } from '@/lib/services';
import { seedDefaultSubAgents } from '@/lib/orchestratorPresets';
import { useSession } from '@/components/SessionProvider';

export default function OrchestratorPage() {
  const toast = useToast();
  const { session } = useSession();
  const scopes = session?.scopes ?? [];
  const isOrchestrator = scopes.includes('orchestrator:manage');
  const { data, loading, error, reload } = useAsync((signal) => listSubAgents({}, signal), []);
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<SubAgent | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);

  async function seedRoster() {
    setSeeding(true);
    try {
      const results = await seedDefaultSubAgents(scopes);
      const failed = results.filter((r) => r.action === 'failed');
      if (failed.length) {
        toast.error(`Seeded with ${failed.length} failure(s): ${failed.map((f) => f.preset).join(', ')}.`);
      } else {
        toast.success('Seeded researcher, writer, and reviewer sub-agents.');
      }
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Seeding failed.');
    } finally {
      setSeeding(false);
    }
  }

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
      className: 'text-right',
      render: (a) => (
        <div className="flex items-center justify-end gap-2">
          <Button size="sm" variant="secondary" disabled={!isOrchestrator} onClick={() => setEditing(a)}>
            Edit Scopes
          </Button>
          {a.status === 'active' ? (
            <Button size="sm" variant="danger" loading={busy === a.agent_id} onClick={() => deactivate(a)}>
              Deactivate
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <Page>
      <PageHeader
        title="Orchestrator"
        description="Your tenant's orchestrator is the only agent that can create sub-agents. Sub-agents inherit a subset of the orchestrator's scopes."
        actions={
          <Link href="/hil" className="text-[13px] font-medium text-brand hover:underline">
            HIL Settings →
          </Link>
        }
      />
      <PageBody fill className="gap-3">
        {!isOrchestrator && (
          <Callout tone="warning" title="Non-Orchestrator Session">
            You are signed in as a non-orchestrator agent. Sub-agent management requires the orchestrator session.
          </Callout>
        )}
        <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <CardHeader
            title="Sub-Agents"
            description="Agents created by this orchestrator."
            actions={
              <div className="flex items-center gap-2">
                <Button
                  size="md"
                  variant="secondary"
                  loading={seeding}
                  disabled={!isOrchestrator}
                  onClick={seedRoster}
                >
                  Seed Default Roster
                </Button>
                <Button size="md" onClick={() => setCreateOpen(true)} disabled={!isOrchestrator}>
                  New Sub-Agent
                </Button>
              </div>
            }
          />
          <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
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
      </PageBody>

      <CreateSubAgentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        orchestratorScopes={scopes}
        onCreated={() => {
          setCreateOpen(false);
          reload();
        }}
      />

      <EditSubAgentScopesModal
        subAgent={editing}
        orchestratorScopes={scopes}
        onClose={() => setEditing(null)}
        onSaved={() => {
          toast.success('Sub-agent scopes updated.');
          setEditing(null);
          reload();
        }}
      />
    </Page>
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
      title="Create Sub-Agent"
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
          <div className="mb-2 text-sm text-muted">Allowed Scopes (subset of orchestrator):</div>
          <ScopeSelector available={orchestratorScopes} value={selected} onChange={setSelected} />
        </div>
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

/**
 * Edit an existing sub-agent's allowed scopes (orchestrator-only). Selection is still bounded to a
 * subset of the orchestrator's own scopes via the ScopeSelector; submit PATCHes the sub-agent.
 */
function EditSubAgentScopesModal({
  subAgent,
  orchestratorScopes,
  onClose,
  onSaved,
}: {
  subAgent: SubAgent | null;
  orchestratorScopes: readonly string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [current, setCurrent] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Seed the selection from the sub-agent's current scopes each time the modal opens.
  useEffect(() => {
    if (subAgent) {
      setCurrent(subAgent.allowed_scopes ?? []);
      setError(null);
    }
  }, [subAgent]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!subAgent) return;
    setBusy(true);
    setError(null);
    try {
      await updateSubAgent(subAgent.agent_id, { allowed_scopes: current });
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={!!subAgent}
      onClose={() => {
        if (!busy) onClose();
      }}
      title="Edit Sub-Agent Scopes"
      description="Scopes are limited to a subset of the orchestrator's own scopes."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="edit-subagent-scopes-form" type="submit" loading={busy}>
            Save Scopes
          </Button>
        </>
      }
    >
      <form id="edit-subagent-scopes-form" onSubmit={submit} className="flex flex-col gap-4">
        <ScopeSelector available={orchestratorScopes} value={current} onChange={setCurrent} />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}
