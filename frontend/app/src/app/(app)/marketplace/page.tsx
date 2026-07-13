'use client';

import { useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { MarketplaceBrowser, type BrowserMcp, type BrowserTool } from '@/components/MarketplaceBrowser';
import { Button, ErrorBanner, Loading, Modal, Select, useToast } from '@/components/ui';
import { getRuntime, listAgents, putRuntime, setToolAccess } from '@/lib/services';
import type { Agent, AgentRuntime, AgentRuntimeRegistration } from '@/lib/types';
import { seedMcpMemberAccess, seedToolMemberAccess } from '@/lib/agentTools';
import { useAsync } from '@/lib/useAsync';
import { BffError } from '@/lib/bff-client';
import { buildInitialRegistration } from '../agents/[agentId]/AgentBuilder';

/**
 * Marketplace — browse the tenant's MCP servers + tools across the Public / Private / Protected
 * sections (the shared {@link MarketplaceBrowser}), and attach any of them to an existing agent
 * (spec A5). "Add to agent…" opens a picker that applies the SAME attach semantics the in-builder
 * picker uses: the MCP's `server_name` joins the agent's `allowed_tools`, and per-capability grants
 * are written to the tool-registry (a whole MCP seeds each member from its default access; a single
 * tool allows only that capability and greys its siblings).
 */

/** What the user chose to attach — an MCP server (all members) or a single tool (one capability). */
type Pending =
  | { kind: 'mcp'; mcp: BrowserMcp }
  | { kind: 'tool'; tool: BrowserTool };

export default function MarketplacePage() {
  const [pending, setPending] = useState<Pending | null>(null);

  return (
    <Page>
      <PageHeader
        title="Marketplace"
        description="Discover MCP servers and tools, then attach them to an agent."
      />
      <PageBody>
        <MarketplaceBrowser
          renderMcpAction={(mcp) => (
            <Button size="sm" variant="secondary" onClick={() => setPending({ kind: 'mcp', mcp })}>
              Add to agent…
            </Button>
          )}
          renderToolAction={(tool) => (
            <Button
              size="sm"
              variant="secondary"
              disabled={!tool.server_name}
              onClick={() => setPending({ kind: 'tool', tool })}
            >
              Add to agent…
            </Button>
          )}
        />
      </PageBody>

      <AddToAgentModal pending={pending} onClose={() => setPending(null)} />
    </Page>
  );
}

/** Normalize the tolerant list-agents envelope (`items` | `agents` | `data`) to a plain array. */
function agentsOf(r: { items?: Agent[]; agents?: Agent[]; data?: Agent[] } | undefined): Agent[] {
  return r?.items ?? r?.agents ?? r?.data ?? [];
}

/** Map an existing runtime to the PUT registration shape, preserving every field. */
function runtimeToRegistration(rt: AgentRuntime): AgentRuntimeRegistration {
  return {
    name: rt.name,
    status: rt.status,
    llm_model: rt.llm_model,
    system_prompt: rt.system_prompt,
    max_tokens: rt.max_tokens,
    temperature: rt.temperature,
    memory_scope: rt.memory_scope,
    guardrail_policy_id: rt.guardrail_policy_id,
    allowed_tools: rt.allowed_tools ?? [],
    tool_loop_enabled: rt.tool_loop_enabled,
    allowed_skills: rt.allowed_skills ?? [],
    allowed_kb_ids: rt.allowed_kb_ids ?? [],
    rag_top_k_per_kb: rt.rag_top_k_per_kb,
    rag_min_score: rt.rag_min_score,
    token_budget_per_task: rt.token_budget_per_task,
  };
}

function AddToAgentModal({ pending, onClose }: { pending: Pending | null; onClose: () => void }) {
  const router = useRouter();
  const toast = useToast();
  const agentsQ = useAsync((signal) => listAgents({ limit: 100 }, signal), []);
  const agents = useMemo(() => agentsOf(agentsQ.data ?? undefined), [agentsQ.data]);
  const [agentId, setAgentId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const open = pending !== null;
  const target = pending?.kind === 'mcp' ? pending.mcp.display_name : pending?.tool.display_name;
  const serverName = pending?.kind === 'mcp' ? pending.mcp.server_name : pending?.tool.server_name ?? null;

  function close() {
    if (busy) return;
    setAgentId('');
    setError(null);
    onClose();
  }

  async function attach() {
    if (!pending || !agentId || !serverName) return;
    const agent = agents.find((a) => a.agent_id === agentId);
    setBusy(true);
    setError(null);
    try {
      // 1) Merge the MCP server into the agent's `allowed_tools` (create the runtime if absent).
      let allowed: string[];
      try {
        const rt = await getRuntime(agentId);
        allowed = rt.allowed_tools ?? [];
        if (!allowed.includes(serverName)) {
          const reg = runtimeToRegistration(rt);
          reg.allowed_tools = [...allowed, serverName];
          const saved = await putRuntime(agentId, reg);
          allowed = saved.allowed_tools ?? reg.allowed_tools;
        }
      } catch (err) {
        if (err instanceof BffError && err.status === 404) {
          const reg = buildInitialRegistration(agent?.name ?? 'Agent', [serverName]);
          const saved = await putRuntime(agentId, reg);
          allowed = saved.allowed_tools ?? reg.allowed_tools;
        } else {
          throw err;
        }
      }

      // 2) Write per-capability grants (same seeding as the in-builder picker).
      const grants =
        pending.kind === 'mcp'
          ? seedMcpMemberAccess(pending.mcp.members)
          : seedToolMemberAccess(pending.tool.members, pending.tool.capability);
      const results = await Promise.allSettled(
        Object.entries(grants).map(([capability, access_mode]) =>
          setToolAccess(serverName, { agent_id: agentId, access_mode, capability }),
        ),
      );
      const failed = results.filter((r) => r.status === 'rejected').length;
      if (failed > 0) {
        throw new Error(`${failed} tool grant${failed === 1 ? '' : 's'} could not be applied.`);
      }

      toast.success(`Added ${target} to ${agent?.name ?? 'the agent'}.`);
      onClose();
      setAgentId('');
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Add to agent"
      description={
        pending?.kind === 'tool'
          ? 'Attaches the tool’s MCP server and allows only this capability for the chosen agent.'
          : 'Attaches this MCP server and allows its member tools for the chosen agent.'
      }
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={attach} loading={busy} disabled={busy || !agentId || !serverName}>
            Add to agent
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {target ? (
          <p className="text-sm text-muted">
            Attaching <span className="font-medium text-fg">{target}</span>{' '}
            <span className="font-mono text-xs">({serverName ?? '—'})</span>.
          </p>
        ) : null}

        {agentsQ.loading ? (
          <Loading label="Loading agents…" />
        ) : agentsQ.error ? (
          <ErrorBanner error={agentsQ.error} title="Could not load your agents" />
        ) : agents.length === 0 ? (
          <p className="text-sm text-muted">
            No agents yet.{' '}
            <button type="button" className="text-brand hover:underline" onClick={() => router.push('/agents')}>
              Create one first.
            </button>
          </p>
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

        {error ? <ErrorBanner error={error} title="Could not add to the agent" /> : null}
      </div>
    </Modal>
  );
}
