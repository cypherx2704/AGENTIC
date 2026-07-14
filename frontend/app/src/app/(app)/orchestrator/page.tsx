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
  Select,
  StatusBadge,
  Table,
  Textarea,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { ScopeSelector } from '@/components/ScopeSelector';
import { useAsync } from '@/lib/useAsync';
import {
  createSubAgent,
  deactivateSubAgent,
  getRuntime,
  listSubAgents,
  putRuntime,
  updateSubAgent,
  type SubAgent,
} from '@/lib/services';
import { memoryScopeFor, runtimeToRegistration, subAgentRegistration } from '@/lib/subAgentRuntime';
import { isRepairable, probeRuntime, runtimeOf, type RuntimeProbe } from '@/lib/runtimeProbe';
import type { AgentRuntime } from '@/lib/types';
import { useSession } from '@/components/SessionProvider';

export default function OrchestratorPage() {
  const toast = useToast();
  const { session } = useSession();
  const scopes = session?.scopes ?? [];
  const isOrchestrator = scopes.includes('orchestrator:manage');
  const { data, loading, error, reload } = useAsync((signal) => listSubAgents({}, signal), []);
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<SubAgent | null>(null);
  const [editingRuntime, setEditingRuntime] = useState<SubAgent | null>(null);
  const [viewing, setViewing] = useState<SubAgent | null>(null);
  // agent_id -> a CLASSIFIED probe of the xAgent runtime. The orchestration roster reads
  // xagent.agents, NOT auth — so an identity with no runtime row can never be scheduled, and that
  // dead end must be visible HERE rather than surfacing as a mystery at run time. The probe is a
  // discriminated union, not a nullable row, because "no runtime" and "not allowed to look" are
  // completely different problems and were previously reported identically (see lib/runtimeProbe).
  const [probes, setProbes] = useState<Record<string, RuntimeProbe>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const items = data?.items;

  // Probe every sub-agent's xAgent runtime whenever the roster changes, so the "Schedulable"
  // column reflects the table the driver actually reads.
  useEffect(() => {
    if (!items?.length) {
      setProbes({});
      return;
    }
    let cancelled = false;
    void (async () => {
      const entries = await Promise.all(
        items.map(async (a) => [a.agent_id, await probeRuntime(a.agent_id)] as const),
      );
      if (!cancelled) setProbes(Object.fromEntries(entries));
    })();
    return () => {
      cancelled = true;
    };
  }, [items]);

  // A 403 on the runtime probe is not an agent problem — it is a SESSION problem, and it affects
  // every agent at once. Say so plainly and once, instead of painting the whole table red and
  // sending the operator off "repairing" agents that were never broken.
  const scopeBlocked = Object.values(probes).some((p) => p.kind === 'forbidden');

  /** Register/reactivate the xAgent runtime for a sub-agent whose identity exists but is unschedulable. */
  async function repair(a: SubAgent) {
    setBusy(a.agent_id);
    try {
      const existing = runtimeOf(probes[a.agent_id]);
      await putRuntime(
        a.agent_id,
        existing
          ? runtimeToRegistration(existing, { status: 'active' })
          : subAgentRegistration({ name: a.name }),
      );
      toast.success(`${a.name} is now schedulable.`);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Repair failed.');
    } finally {
      setBusy(null);
    }
  }

  async function deactivate(a: SubAgent) {
    setBusy(a.agent_id);
    try {
      await deactivateSubAgent(a.agent_id);

      // Mirror the deactivation into the xAgent runtime. The orchestration roster reads
      // xagent.agents (status='active'), NOT auth — without this the "deactivated" sub-agent
      // would keep being scheduled. A sub-agent with no runtime row has nothing to mirror.
      let runtime: AgentRuntime | null = null;
      try {
        runtime = await getRuntime(a.agent_id);
      } catch {
        runtime = null; // never registered a runtime — not in the roster anyway
      }
      if (runtime && runtime.status !== 'inactive') {
        await putRuntime(a.agent_id, runtimeToRegistration(runtime, { status: 'inactive' }));
      }

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
    {
      // The routing description is what the orchestrator's planner reads to decide which steps come
      // here. An empty one is a real defect — the planner is reduced to guessing from the name — so
      // surface it in the roster instead of letting it silently misroute work at run time.
      key: 'description',
      header: 'When to use',
      render: (a) => {
        const p = probes[a.agent_id];
        if (!p) return <span className="text-xs text-faint">checking…</span>;
        const desc = runtimeOf(p)?.description?.trim();
        if (p.kind === 'forbidden' || p.kind === 'error') return <span className="text-xs text-faint">—</span>;
        if (!desc) return <Badge tone="warning">Not described</Badge>;
        return (
          <span className="line-clamp-2 max-w-[26ch] text-xs text-muted" title={desc}>
            {desc}
          </span>
        );
      },
    },
    {
      // The TOOLS are the other half of what the planner routes on: a step needing external data can
      // only go to an agent that actually holds a tool able to fetch it.
      key: 'tools',
      header: 'Tools',
      render: (a) => {
        const rt = runtimeOf(probes[a.agent_id]);
        if (!probes[a.agent_id]) return <span className="text-xs text-faint">checking…</span>;
        if (!rt) return <span className="text-xs text-faint">—</span>;
        if (rt.allowed_tools.length === 0) return <span className="text-xs text-faint">none</span>;
        return (
          <div className="flex flex-wrap gap-1">
            {rt.allowed_tools.slice(0, 2).map((t) => (
              <Badge key={t}>{t}</Badge>
            ))}
            {rt.allowed_tools.length > 2 ? (
              <span className="text-xs text-muted">+{rt.allowed_tools.length - 2}</span>
            ) : null}
          </div>
        );
      },
    },
    { key: 'status', header: 'Status', render: (a) => <StatusBadge status={a.status} /> },
    {
      key: 'schedulable',
      header: 'Schedulable',
      render: (a) => {
        if (a.status !== 'active') return <span className="text-xs text-faint">—</span>;
        const p = probes[a.agent_id];
        if (!p) return <span className="text-xs text-faint">checking…</span>;
        switch (p.kind) {
          case 'ready':
            return <Badge tone="success">Ready</Badge>;
          case 'inactive':
            return <Badge tone="warning">Runtime {p.runtime.status}</Badge>;
          case 'missing':
            return <Badge tone="danger">No runtime</Badge>;
          // NOT "No runtime". We were refused permission to look — the agent may be perfectly fine,
          // and "Repair" would 403 on the same scope gate.
          case 'forbidden':
            return (
              <span title={p.message}>
                <Badge tone="warning">Can&apos;t check — no scope</Badge>
              </span>
            );
          default:
            return (
              <span title={`${p.code}: ${p.message}`}>
                <Badge tone="warning">Check failed ({p.status || 'network'})</Badge>
              </span>
            );
        }
      },
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (a) => (
        <div className="flex items-center justify-end gap-2">
          {/* Repair only when it can actually help. A 403/5xx is not about this agent, and a Repair
              would just 403 again on the same gate — offering it there is a wild goose chase. */}
          {a.status === 'active' && isRepairable(probes[a.agent_id]) ? (
            <Button size="sm" disabled={!isOrchestrator} loading={busy === a.agent_id} onClick={() => repair(a)}>
              Repair
            </Button>
          ) : null}
          <Button size="sm" variant="secondary" onClick={() => setViewing(a)}>
            Details
          </Button>
          <Button size="sm" variant="secondary" disabled={!isOrchestrator} onClick={() => setEditingRuntime(a)}>
            Edit
          </Button>
          <Button size="sm" variant="secondary" disabled={!isOrchestrator} onClick={() => setEditing(a)}>
            Scopes
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
        {scopeBlocked && (
          <Callout tone="warning" title="This session cannot read agent runtimes">
            <p>
              Reading or writing a sub-agent&apos;s runtime needs the <code>agent:admin</code> (or{' '}
              <code>platform:admin</code>) scope, and this session has neither — so the{' '}
              <strong>Schedulable</strong> and <strong>When to use</strong> columns cannot be filled in.
            </p>
            <p className="mt-1">
              This does <strong>not</strong> mean the agents are broken: they may be registered and running
              fine. Re-authenticate with an admin scope to manage them — <strong>Repair</strong> would fail on
              the same gate.
            </p>
          </Callout>
        )}
        <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <CardHeader
            title="Sub-Agents"
            description="Agents created by this orchestrator."
            actions={
              <Button size="md" onClick={() => setCreateOpen(true)} disabled={!isOrchestrator}>
                New Sub-Agent
              </Button>
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

      <SubAgentDetailsModal
        subAgent={viewing}
        probe={viewing ? probes[viewing.agent_id] : undefined}
        onClose={() => setViewing(null)}
        onEdit={() => {
          const a = viewing;
          setViewing(null);
          setEditingRuntime(a);
        }}
      />

      <EditSubAgentRuntimeModal
        subAgent={editingRuntime}
        runtime={editingRuntime ? runtimeOf(probes[editingRuntime.agent_id]) : null}
        onClose={() => setEditingRuntime(null)}
        onSaved={() => {
          toast.success('Sub-agent updated.');
          setEditingRuntime(null);
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

/**
 * Everything about one sub-agent — led by THE THING THAT DECIDES ITS FATE.
 *
 * The headline is not the model or the prompt: it is the **capability catalogue entry**, rendered
 * exactly as the orchestrator's planner is shown it. That block (name + "use when" + tools) is the
 * entire basis on which a step is routed here, so seeing it verbatim answers the only question that
 * really matters — *would I route work to this agent, reading only this?* An empty description or an
 * empty tool list stops being an abstract warning and becomes self-evidently unroutable.
 */
function SubAgentDetailsModal({
  subAgent,
  probe,
  onClose,
  onEdit,
}: {
  subAgent: SubAgent | null;
  probe: RuntimeProbe | undefined;
  onClose: () => void;
  onEdit: () => void;
}) {
  if (!subAgent) return null;
  const rt = runtimeOf(probe);
  const description = rt?.description?.trim();
  const tools = rt?.allowed_tools ?? [];

  return (
    <Modal
      open={!!subAgent}
      onClose={onClose}
      title={subAgent.name}
      description="What the orchestrator knows about this agent — and what it can actually do."
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
          <Button onClick={onEdit}>Edit</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {probe?.kind === 'forbidden' || probe?.kind === 'error' ? (
          <Callout tone="warning" title="Runtime could not be read">
            {probe.kind === 'forbidden'
              ? 'This session lacks the agent:admin scope, so the runtime config below cannot be shown. The agent itself may be perfectly healthy.'
              : `${probe.code}: ${probe.message}`}
          </Callout>
        ) : null}

        {probe?.kind === 'missing' ? (
          <Callout tone="danger" title="Not schedulable — no runtime">
            This agent exists in Auth but has no xAgent runtime row. The orchestration roster reads that
            table, so <strong>the planner cannot see this agent at all</strong> and will never delegate to
            it. Use <strong>Repair</strong> or <strong>Edit</strong> to register it.
          </Callout>
        ) : null}

        {/* ── the routing catalogue entry ─────────────────────────────────────────────── */}
        <section>
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">
            How the orchestrator sees this agent when routing
          </p>
          <pre className="overflow-x-auto rounded-md border border-border bg-surface-2 px-3 py-2.5 font-mono text-xs leading-relaxed text-fg/90">
{`- ${subAgent.name}
    use when: ${description || 'UNSPECIFIED — no description was configured for this agent'}
    tools: ${tools.length ? tools.join(', ') : 'NONE (cannot call any tool)'}`}
          </pre>
          {!description ? (
            <p className="mt-1.5 text-xs text-warning">
              With no description, the planner can only guess from the name — it will misroute work here,
              or avoid the agent entirely.
            </p>
          ) : null}
          {tools.length === 0 ? (
            <p className="mt-1 text-xs text-muted">
              No tools: this agent answers from the model alone. Correct for a writer — but it can never
              fetch external data, and the planner is told so.
            </p>
          ) : null}
          <p className="mt-2 text-xs text-muted">
            Attaching a tool takes <strong>two</strong> writes — the runtime&apos;s <code>allowed_tools</code>{' '}
            <em>and</em> a tool-registry access grant. An agent listed against a tool it was never granted
            gets <code>TOOL_DENIED</code> at run time. Do both in the Agent Builder:{' '}
            <Link href={`/agents/${subAgent.agent_id}`} className="font-medium text-brand hover:underline">
              Attach tools →
            </Link>
          </p>
        </section>

        {/* ── the rest of the runtime ─────────────────────────────────────────────────── */}
        {rt ? (
          <section className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <Detail label="Model" value={rt.llm_model} />
            <Detail label="Runtime status" value={rt.status} />
            <Detail label="Tool loop" value={rt.tool_loop_enabled === false ? 'off (single LLM call)' : 'on'} />
            <Detail label="Memory scope" value={rt.memory_scope} />
            <Detail label="Max tokens" value={String(rt.max_tokens)} />
            <Detail label="Token budget / task" value={String(rt.token_budget_per_task)} />
          </section>
        ) : null}

        {rt?.system_prompt ? (
          <section>
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">
              System prompt — its own instructions, once a step reaches it
            </p>
            <p className="max-h-32 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-fg/90">
              {rt.system_prompt}
            </p>
          </section>
        ) : null}

        <section>
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">
            Scopes ({subAgent.allowed_scopes.length}) — always a subset of the orchestrator&apos;s
          </p>
          <div className="flex flex-wrap gap-1">
            {subAgent.allowed_scopes.length === 0 ? (
              <span className="text-xs text-faint">none</span>
            ) : (
              subAgent.allowed_scopes.map((s) => <Badge key={s}>{s}</Badge>)
            )}
          </div>
        </section>

        <p className="font-mono text-[11px] text-faint">agent_id: {subAgent.agent_id}</p>
      </div>
    </Modal>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wider text-faint">{label}</p>
      <p className="mt-0.5 font-medium text-fg">{value}</p>
    </div>
  );
}

/**
 * Edit a sub-agent's ROUTING description (and its own instructions / model).
 *
 * This exists because `description` is what the orchestrator's planner routes on, and there was
 * previously no way to set it after creation. A sub-agent whose runtime row was created by the
 * "Repair" path has no description to recover — it would have stayed permanently undescribed, and
 * the planner would have been left guessing at its name forever.
 *
 * Doubles as the repair path: when the agent has NO runtime row at all (`runtime === null`), saving
 * REGISTERS one (active, so it becomes roster-eligible) instead of updating it — so an operator can
 * never end up with a schedulable-but-undescribed agent.
 */
function EditSubAgentRuntimeModal({
  subAgent,
  runtime,
  onClose,
  onSaved,
}: {
  subAgent: SubAgent | null;
  runtime: AgentRuntime | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [description, setDescription] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [model, setModel] = useState('smart');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Re-seed from the live row each time the modal opens, so an edit never starts from stale state.
  useEffect(() => {
    if (!subAgent) return;
    setDescription(runtime?.description ?? '');
    setSystemPrompt(runtime?.system_prompt ?? '');
    setModel(runtime?.llm_model ?? 'smart');
    setError(null);
  }, [subAgent, runtime]);

  if (!subAgent) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!subAgent) return;
    setBusy(true);
    setError(null);
    try {
      const body = runtime
        ? runtimeToRegistration(runtime, {
            description: description.trim(),
            system_prompt: systemPrompt.trim() || runtime.system_prompt,
            llm_model: model,
            status: 'active',
          })
        : // No runtime row yet (an identity that was never registered, or a half-repaired one):
          // register a complete, active one now rather than leaving it unschedulable.
          subAgentRegistration({
            name: subAgent.name,
            description: description.trim(),
            llm_model: model,
            ...(systemPrompt.trim() ? { system_prompt: systemPrompt.trim() } : {}),
          });
      await putRuntime(subAgent.agent_id, body);
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
      onClose={onClose}
      title={`Edit ${subAgent.name}`}
      description="How the orchestrator decides to route work here, and how this agent behaves once it does."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            form="edit-subagent-runtime-form"
            type="submit"
            loading={busy}
            disabled={!description.trim()}
          >
            Save
          </Button>
        </>
      }
    >
      <form id="edit-subagent-runtime-form" onSubmit={submit} className="flex flex-col gap-4">
        {!runtime ? (
          <Callout tone="warning" title="Not registered">
            This sub-agent has no xAgent runtime, so it cannot be scheduled. Saving will register it.
          </Callout>
        ) : null}
        <Textarea
          label="When to use this agent"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          placeholder="Fetches stars, open issues and release history for a GitHub repository."
          hint="Written for the ORCHESTRATOR, not the agent. It reads this — plus the tools attached to this agent — to decide which steps to send here. A vague description gets it the wrong work."
          required
        />
        <Select label="Model" value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="smart">smart — higher quality</option>
          <option value="fast">fast — cheaper / quicker</option>
        </Select>
        <Textarea
          label="System Prompt"
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          rows={4}
          placeholder="You are a helpful assistant. Answer concisely."
          hint="This sub-agent's own instructions — how it behaves once a step reaches it."
        />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
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
  const [description, setDescription] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [model, setModel] = useState('smart');
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const trimmed = name.trim();
    try {
      // 1. Identity (auth): agent_type='sub_agent' + parent_orchestrator_id=caller.
      const sub = await createSubAgent({ name: trimmed, allowed_scopes: selected });

      // 2. Runtime (xAgent): REQUIRED. The orchestration roster reads xagent.agents — an auth
      //    identity with no runtime row can never be scheduled (every run would fail
      //    UNASSIGNED_NODE). The PUT create-path stamps agent_type/parent from auth, so this
      //    is what makes the sub-agent visible to the orchestrator.
      await putRuntime(
        sub.agent_id,
        subAgentRegistration({
          name: trimmed,
          // What the orchestrator's planner ROUTES on, alongside the tools attached below. Required
          // — an undescribed sub-agent can only be routed to by guessing at its name.
          description: description.trim(),
          llm_model: model,
          // Keep memory_scope consistent with the scopes actually granted: memory enabled without
          // `mem:write` would 403 on every task, silently (the write stage is fail-soft).
          memory_scope: memoryScopeFor(selected),
          ...(systemPrompt.trim() ? { system_prompt: systemPrompt.trim() } : {}),
        }),
      );

      onCreated();
      setName('');
      setDescription('');
      setSystemPrompt('');
      setModel('smart');
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
          <Button
            form="create-subagent-form"
            type="submit"
            loading={busy}
            disabled={!name.trim() || !description.trim() || !selected.length}
          >
            Create
          </Button>
        </>
      }
    >
      <form id="create-subagent-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          hint="The name the orchestrator's plan binds to. Call it whatever fits its job."
          required
        />
        <Textarea
          label="When to use this agent"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          placeholder="Fetches stars, open issues and release history for a GitHub repository."
          hint="Written for the ORCHESTRATOR, not the agent. It reads this — plus the tools you attach — to decide which steps to send here. Be concrete about what this agent is for; a vague description gets it the wrong work."
          required
        />
        <Select label="Model" value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="smart">smart — higher quality</option>
          <option value="fast">fast — cheaper / quicker</option>
        </Select>
        <Textarea
          label="System Prompt"
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          rows={4}
          placeholder="You are a helpful assistant. Answer concisely."
          hint="This sub-agent's own instructions — how it should behave once a step reaches it. Leave blank for a sensible default."
        />
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
