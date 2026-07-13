'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentList } from '@/components/AgentList';
import { ScopeSelector } from '@/components/ScopeSelector';
import { AgentToolPicker, type AgentToolGrant } from '@/components/AgentToolPicker';
import { useSession } from '@/components/SessionProvider';
import { Button, ErrorBanner, Input, Modal, useToast } from '@/components/ui';
import { createAgent, putRuntime, setToolAccess } from '@/lib/services';
import type { Agent } from '@/lib/types';
import { buildInitialRegistration } from './[agentId]/AgentBuilder';

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
  const router = useRouter();
  const { session } = useSession();
  const [name, setName] = useState('');
  const [selectedScopes, setSelectedScopes] = useState<string[]>([]);
  // DEFERRED tool picker: MCP server names -> allowed_tools; grants -> per-capability access.
  const [servers, setServers] = useState<string[]>([]);
  const [grants, setGrants] = useState<AgentToolGrant[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Partial success: the agent identity was created but seeding its runtime / tool grants failed.
  // We must NOT report full success — keep the modal open and point the user at the edit page.
  const [partial, setPartial] = useState<{ agent: Agent; error: unknown } | null>(null);

  // Clear any stale error / partial state each time the modal is (re)opened.
  useEffect(() => {
    if (open) {
      setError(null);
      setPartial(null);
    }
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setPartial(null);

    // Step 1 — create the identity. A failure here means nothing exists yet; surface it plainly.
    // Any attached tool needs the coarse tool:invoke scope; add it so the agent can actually call.
    const hasTools = servers.length > 0 || grants.length > 0;
    const scopes =
      hasTools && !selectedScopes.includes('tool:invoke')
        ? [...selectedScopes, 'tool:invoke']
        : selectedScopes;
    let agent: Agent;
    try {
      agent = await createAgent({ name: name.trim(), allowed_scopes: scopes });
    } catch (err) {
      setError(err);
      setBusy(false);
      return;
    }

    // Step 2 — apply the deferred selections to BOTH tool stores now that the agent exists:
    //  1) seed `allowed_tools` (the MCP server names) on the xAgent runtime, then
    //  2) commit EVERY collected grant with its own mode — `automated` for allowed members AND
    //     explicit `none` for greyed siblings (mirroring the LIVE path) so a sibling never falls
    //     back to the registry's permissive `default_access_mode`.
    // The agent row already exists, so a rejection here is PARTIAL: don't pretend full success.
    try {
      if (servers.length > 0) {
        await putRuntime(agent.agent_id, buildInitialRegistration(name.trim(), servers));
      }
      const results = await Promise.allSettled(
        grants.map((g) =>
          setToolAccess(g.server_name, {
            agent_id: agent.agent_id,
            access_mode: g.access_mode,
            capability: g.capability,
          }),
        ),
      );
      const failed = results.filter((r) => r.status === 'rejected').length;
      if (failed > 0) {
        throw new Error(`${failed} tool grant${failed === 1 ? '' : 's'} could not be applied.`);
      }
      onCreated(agent);
    } catch (err) {
      setPartial({ agent, error: err });
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
        partial ? (
          <>
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
            <Button onClick={() => router.push(`/agents/${partial.agent.agent_id}`)}>
              Finish in agent settings
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button form="create-agent-form" type="submit" loading={busy} disabled={!name.trim()}>
              Create
            </Button>
          </>
        )
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
        <div className="border-t border-border pt-4">
          <AgentToolPicker servers={servers} onServersChange={setServers} onGrantsChange={setGrants} />
        </div>
        {partial ? (
          <ErrorBanner
            error={partial.error}
            title={`Agent "${partial.agent.name}" was created, but its tools weren't fully set up — open its settings to finish.`}
          />
        ) : error ? (
          <ErrorBanner error={error} />
        ) : null}
      </form>
    </Modal>
  );
}
