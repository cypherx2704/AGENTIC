'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentList } from '@/components/AgentList';
import { ScopeSelector } from '@/components/ScopeSelector';
import { useSession } from '@/components/SessionProvider';
import { Button, ErrorBanner, Input, Modal, useToast } from '@/components/ui';
import { createAgent } from '@/lib/services';
import type { Agent } from '@/lib/types';

export default function AgentsPage() {
  const router = useRouter();
  const toast = useToast();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <Page>
      <PageHeader
        title="Agents"
        description="Identity + runtime configuration for every agent in this tenant."
        actions={
          <Button onClick={() => setCreateOpen(true)} size="md">
            New Agent
          </Button>
        }
      />

      <PageBody>
        <AgentList onSelect={(a) => router.push(`/agents/${a.agent_id}`)} fallback={<OpenById />} />
      </PageBody>

      <CreateAgentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(agent) => {
          toast.success(`Agent "${agent.name}" created.`);
          setCreateOpen(false);
          router.push(`/agents/${agent.agent_id}`);
        }}
      />
    </Page>
  );
}

/** Direct open-by-id (the auth service reads agents by id; also a fallback if the list fails to load). */
function OpenById() {
  const router = useRouter();
  const [id, setId] = useState('');
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (id.trim()) router.push(`/agents/${id.trim()}`);
      }}
      className="flex items-end gap-2"
    >
      <Input placeholder="agent id…" value={id} onChange={(e) => setId(e.target.value)} className="w-72" />
      <Button type="submit" size="md" variant="secondary" disabled={!id.trim()}>
        Open
      </Button>
    </form>
  );
}

function CreateAgentModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (a: Agent) => void;
}) {
  const { session } = useSession();
  const [name, setName] = useState('');
  const [selectedScopes, setSelectedScopes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const agent = await createAgent({
        name: name.trim(),
        allowed_scopes: selectedScopes,
      });
      onCreated(agent);
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
      title="Create Agent"
      description="Registers an identity in Auth. Configure its runtime next."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="create-agent-form" type="submit" loading={busy} disabled={!name.trim()}>
            Create
          </Button>
        </>
      }
    >
      <form id="create-agent-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus />
        <div>
          <p className="text-sm font-medium text-fg">Allowed Scopes</p>
          <p className="mb-2 mt-0.5 text-xs text-muted">
            These bound what keys for this agent can be granted.
          </p>
          <ScopeSelector
            available={session?.scopes ?? []}
            value={selectedScopes}
            onChange={setSelectedScopes}
          />
        </div>
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}
