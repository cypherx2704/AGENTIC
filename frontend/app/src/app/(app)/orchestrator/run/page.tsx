'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { ExecutionTree } from '@/components/ExecutionTree';
import { PendingApprovals } from '@/components/PendingApprovals';
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
  Switch,
  Textarea,
  useToast,
} from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { useSession } from '@/components/SessionProvider';
import {
  cancelOrchestration,
  getOrchestrationGraph,
  listSubAgents,
  orchestrationStreamUrl,
  submitOrchestration,
  type OrchestrationGraph,
} from '@/lib/services';

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'timeout']);

/** Heuristic: does the prompt ask for sub-agents even though the toggle is off? (Requirement #6.) */
function promptWantsSubAgents(text: string): boolean {
  return /\bsub-?agents?\b|\bdelegate\b|\bin parallel\b|\bmultiple agents\b|\bspecialists?\b/i.test(text);
}

/**
 * Orchestrator Run — PROMPT → ORCHESTRATOR → SUB-AGENTS. Submit a goal; when "Use sub-agents" is on
 * the orchestrator decomposes it and fans out to its sub-agents, streaming a live execution tree.
 * Toggle off = the orchestrator answers alone (use the single-agent Task Runner for that).
 */
export default function OrchestratorRunPage() {
  const toast = useToast();
  const { session } = useSession();
  const isOrchestrator = (session?.scopes ?? []).includes('orchestrator:manage');

  const [goal, setGoal] = useState('');
  const [useSubAgents, setUseSubAgents] = useState(true);
  // Independent of useSubAgents: this governs whether ANY agent in the run may call a tool.
  // Both off = a plain chatbot (no planner, no roster, no tools).
  const [useTools, setUseTools] = useState(true);
  const [costBudget, setCostBudget] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [graph, setGraph] = useState<OrchestrationGraph | null>(null);
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [gateOpen, setGateOpen] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  const { data: roster } = useAsync((signal) => listSubAgents({ limit: 100 }, signal), []);
  const subAgents = roster?.items ?? [];

  const closeStream = useCallback(() => {
    esRef.current?.close();
    esRef.current = null;
    setStreaming(false);
  }, []);

  useEffect(() => () => closeStream(), [closeStream]);

  function openStream(id: string) {
    closeStream();
    setStreaming(true);
    const es = new EventSource(orchestrationStreamUrl(id), { withCredentials: true });
    esRef.current = es;
    es.addEventListener('run', (e) => {
      const snap = safeParse((e as MessageEvent).data);
      if (snap?.workflow) setGraph(snap as OrchestrationGraph);
    });
    const finalize = async () => {
      closeStream();
      try {
        setGraph(await getOrchestrationGraph(id)); // canonical terminal state
      } catch {
        /* keep the last streamed frame */
      }
    };
    es.addEventListener('done', () => void finalize());
    es.addEventListener('error', (e) => {
      // A server terminal frame carries data; a PERMANENT close (readyState CLOSED — bad MIME / non-2xx
      // / unrecoverable) must also unstick the UI. A transient drop (CONNECTING) auto-reconnects — leave it.
      if ((e as MessageEvent).data || es.readyState === EventSource.CLOSED) void finalize();
    });
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    // Requirement #6: toggle OFF but the prompt asks for sub-agents → pause on a HIL-style settings gate.
    if (!useSubAgents && promptWantsSubAgents(goal)) {
      setGateOpen(true);
      return;
    }
    void doSubmit(useSubAgents ? 'subagents' : 'solo');
  }

  async function doSubmit(mode: 'subagents' | 'solo') {
    setBusy(true);
    setError(null);
    setGraph(null);
    setWorkflowId(null);
    closeStream();
    try {
      const budgetNum = Number(costBudget);
      const resp = await submitOrchestration({
        goal: goal.trim(),
        mode,
        use_tools: useTools,
        // Only send a strictly-positive budget — the backend validates gt=0, so 0/blank ⇒ omit (unlimited).
        cost_budget_usd: costBudget.trim() && Number.isFinite(budgetNum) && budgetNum > 0 ? budgetNum : undefined,
      });
      setWorkflowId(resp.workflow_id);
      openStream(resp.workflow_id);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  const status = graph?.workflow.status;
  const canCancel = !!workflowId && (streaming || (status !== undefined && !TERMINAL.has(status)));

  async function onCancel() {
    if (!workflowId) return;
    setCancelling(true);
    try {
      await cancelOrchestration(workflowId);
      toast.success('Cancel requested.');
    } catch {
      toast.error('Could not cancel this run.');
    } finally {
      setCancelling(false);
    }
  }

  return (
    <Page>
      <PageHeader
        title="Orchestrator Run"
        description="Give the orchestrator a goal — it decomposes and delegates to your sub-agents, live."
        actions={
          <Link href="/orchestrator" className="text-[13px] font-medium text-brand hover:underline">
            Manage Sub-Agents →
          </Link>
        }
      />
      <PageBody>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Card>
            <CardHeader title="Goal" />
            <CardBody>
              {!isOrchestrator && (
                <Callout tone="warning" title="Non-Orchestrator Session" className="mb-3">
                  Running orchestrations requires the orchestrator session (scope orchestrator:manage).
                </Callout>
              )}
              <form onSubmit={onSubmit} className="flex flex-col gap-4">
                <Textarea
                  label="Prompt"
                  placeholder="e.g. Research the latest in retrieval-augmented generation and write a brief."
                  value={goal}
                  onChange={(ev) => setGoal(ev.target.value)}
                  required
                />
                <div className="flex flex-col gap-3 rounded-md border border-border bg-surface-2 px-3.5 py-3">
                  <Switch
                    checked={useSubAgents}
                    onChange={setUseSubAgents}
                    label="Use Sub-Agents"
                    hint="On = decompose + delegate to sub-agents. Off = the orchestrator answers alone."
                  />
                  <Switch
                    checked={useTools}
                    onChange={setUseTools}
                    label="Use Tools"
                    hint="On = agents may call their tools; the LLM decides when. Off = no tool is offered or invoked."
                  />
                  {!useTools && !useSubAgents ? (
                    <p className="text-xs text-muted">
                      Plain chat: no planner, no sub-agents, no tools — the orchestrator answers from its own
                      knowledge.
                    </p>
                  ) : null}
                </div>
                <Input
                  label="Cost budget (USD)"
                  type="number"
                  min={0}
                  step="0.01"
                  placeholder="optional — stop early if exceeded"
                  value={costBudget}
                  onChange={(ev) => setCostBudget(ev.target.value)}
                  hint="A per-run ceiling; the run early-stops when crossed."
                />
                <div className="flex items-center gap-2">
                  <Button type="submit" size="md" loading={busy} disabled={!isOrchestrator || !goal.trim()}>
                    Run
                  </Button>
                  {useSubAgents ? <Badge tone="info">sub-agents</Badge> : <Badge>solo</Badge>}
                  {useTools ? <Badge tone="info">tools</Badge> : <Badge>no tools</Badge>}
                </div>
                {error ? <ErrorBanner error={error} /> : null}
              </form>

              <div className="mt-5">
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-faint">
                  Available Sub-Agents ({subAgents.length})
                </p>
                {subAgents.length === 0 ? (
                  <p className="text-sm text-muted">
                    No sub-agents yet.{' '}
                    <Link href="/orchestrator" className="text-brand hover:underline">
                      Create some
                    </Link>{' '}
                    so the orchestrator has specialists to delegate to.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {subAgents.map((a) => (
                      <Badge key={a.agent_id}>{a.name}</Badge>
                    ))}
                  </div>
                )}
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Execution"
              actions={
                <div className="flex items-center gap-2">
                  {canCancel && (
                    <Button variant="secondary" size="sm" loading={cancelling} onClick={() => void onCancel()}>
                      Cancel
                    </Button>
                  )}
                  {streaming ? <Badge tone="warning">Streaming…</Badge> : null}
                </div>
              }
            />
            <CardBody className="flex flex-col gap-3">
              {workflowId ? (
                <PendingApprovals
                  workflowId={workflowId}
                  active={
                    !!graph &&
                    (graph.workflow.status === 'awaiting_approval' ||
                      graph.nodes.some((n) => n.status === 'awaiting_approval'))
                  }
                />
              ) : null}
              {busy && !graph ? <Loading label="Submitting…" /> : <ExecutionTree graph={graph} />}
            </CardBody>
          </Card>
        </div>
      </PageBody>

      <Modal
        open={gateOpen}
        onClose={() => setGateOpen(false)}
        title="Use sub-agents for this run?"
        description="Your prompt asks for sub-agents, but “Use Sub-Agents” is off."
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setGateOpen(false);
                void doSubmit('solo');
              }}
            >
              Run Solo Anyway
            </Button>
            <Button
              onClick={() => {
                setUseSubAgents(true);
                setGateOpen(false);
                void doSubmit('subagents');
              }}
            >
              Enable Sub-Agents & Run
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted">
            The orchestrator can delegate to {subAgents.length} sub-agent{subAgents.length === 1 ? '' : 's'}:
          </p>
          {subAgents.length === 0 ? (
            <Callout tone="warning" title="No sub-agents configured">
              Create sub-agents on the Orchestrator page first, or run solo.
            </Callout>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {subAgents.map((a) => (
                <Badge key={a.agent_id}>{a.name}</Badge>
              ))}
            </div>
          )}
          <p className="text-xs text-faint">
            Fan-out, depth and cost are capped by the orchestrator&apos;s limits; a per-run budget can stop it early.
          </p>
        </div>
      </Modal>
    </Page>
  );
}

function safeParse(raw: string): any {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
