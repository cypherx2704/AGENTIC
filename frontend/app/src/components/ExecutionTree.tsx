'use client';

import { useState } from 'react';
import { Badge, Card, CardBody, CardHeader, StatusBadge } from '@/components/ui';
import { AgentName } from '@/components/AgentNames';
import { TaskTimeline } from '@/components/TaskTimeline';
import type { OrchestrationGraph, OrchestrationNode } from '@/lib/services';
import type { TaskStep } from '@/lib/types';
import { formatCost, formatDuration, formatNumber } from '@/lib/utils';

/**
 * The live execution tree for an orchestration run.
 *
 * It answers three questions the previous version could not:
 *
 *   1. **What did each sub-agent actually DO?** Every node carries its own pipeline audit trail
 *      (guardrail → llm → tool_call → …), rendered with the very same <TaskTimeline> the single-agent
 *      Task Runner uses. A sub-agent's tool calls are no longer invisible — they are the point.
 *   2. **What ran in PARALLEL?** Nodes are grouped into WAVES (a topological layering of
 *      `depends_on`), which is literally how the driver schedules them: one wave at a time, every
 *      node inside a wave concurrently. A flat list threw that shape away — a 3-way fan-out looked
 *      exactly like 3 sequential steps.
 *   3. **What is happening RIGHT NOW?** Steps arrive on the SSE frames, so a running sub-agent's
 *      tools light up as it calls them rather than appearing (if at all) once it has finished.
 *
 * The steps arrive INLINE on the graph payload. This component deliberately fetches nothing of its
 * own: the previous version lazily pulled each node's task on expand, which was an N+1 that grew with
 * fan-out, latched its cache on the first (usually empty) response, and — because a node's `task_id`
 * was only stamped at completion — could show nothing at all while the node was still working.
 */
export function ExecutionTree({ graph }: { graph: OrchestrationGraph | null }) {
  if (!graph) {
    return <p className="py-8 text-center text-sm text-muted">Submit a goal to watch the orchestration.</p>;
  }
  const { workflow, nodes } = graph;
  const waves = toWaves(nodes);
  const toolCount = nodes.reduce((n, node) => n + toolCallsOf(node).length, 0);
  const delegated = nodes.filter((n) => n.preset !== 'orchestrator');

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Stat label="Status" value={<StatusBadge status={workflow.status} />} />
        <Stat label="Steps" value={String(nodes.length)} />
        <Stat label="Tool calls" value={String(toolCount)} />
        <Stat label="Tokens" value={formatNumber(workflow.tokens_used ?? 0)} />
        <Stat
          label="Cost"
          value={
            <span>
              {formatCost(workflow.cost_usd ?? 0)}
              {workflow.cost_budget_usd ? (
                <span className="text-xs text-muted"> / {formatCost(workflow.cost_budget_usd)}</span>
              ) : null}
            </span>
          }
        />
      </div>

      <Card className="overflow-hidden">
        <CardHeader
          title="Execution Flow"
          description={
            nodes.length === 0
              ? undefined
              : delegated.length === 0
                ? 'The orchestrator answered this itself — no delegation was needed.'
                : `${delegated.length} sub-agent step${delegated.length === 1 ? '' : 's'} across ${waves.length} wave${waves.length === 1 ? '' : 's'}`
          }
        />
        <CardBody className="p-0">
          {nodes.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-muted">Planning…</p>
              <p className="mt-1 text-xs text-faint">
                The orchestrator is deciding which sub-agents — if any — this goal needs.
              </p>
            </div>
          ) : (
            <div className="flex flex-col">
              <PlanRow decomposition={workflow.decomposition} waveCount={waves.length} />
              {waves.map((wave, i) => (
                <WaveBlock key={i} index={i} wave={wave} total={waves.length} />
              ))}
            </div>
          )}
        </CardBody>
      </Card>

      {workflow.output?.message ? (
        <Card>
          <CardHeader
            title="Final Answer"
            description="Synthesized by the orchestrator from each step's summary."
          />
          <CardBody>
            <p className="whitespace-pre-wrap text-sm text-fg">{workflow.output.message}</p>
          </CardBody>
        </Card>
      ) : null}

      {workflow.error_code ? (
        <div className="rounded-md border border-danger/40 bg-danger/10 px-4 py-3">
          <Badge tone="danger">{workflow.error_code}</Badge>
          {workflow.error_msg ? <p className="mt-1 text-sm text-fg/90">{workflow.error_msg}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

/** The orchestrator's own planning pass — the step that produced everything below it. */
function PlanRow({ decomposition, waveCount }: { decomposition?: string | null; waveCount: number }) {
  const planned = decomposition === 'llm';
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border bg-surface-2 px-4 py-2.5">
      <span className="h-2 w-2 shrink-0 rounded-full bg-brand" />
      <span className="text-sm font-medium text-fg">Plan</span>
      <span className="text-xs text-muted">
        {planned
          ? 'the orchestrator LLM planned this run and chose each agent'
          : 'no planning needed — a single step run by the orchestrator'}
      </span>
      <span className="ml-auto font-mono text-[11px] text-faint">
        {waveCount} wave{waveCount === 1 ? '' : 's'}
      </span>
    </div>
  );
}

/** One scheduling wave: everything inside it ran CONCURRENTLY — that is how the driver runs it. */
function WaveBlock({ index, wave, total }: { index: number; wave: OrchestrationNode[]; total: number }) {
  return (
    <div className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-2 px-4 py-1.5">
        <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-faint">
          Wave {index + 1}/{total}
        </span>
        {wave.length > 1 ? (
          <Badge tone="info">{wave.length} in parallel</Badge>
        ) : (
          <span className="text-[11px] text-faint">sequential</span>
        )}
      </div>
      <ul className="divide-y divide-border border-t border-border">
        {wave.map((n) => (
          <NodeRow key={n.node_id} node={n} />
        ))}
      </ul>
    </div>
  );
}

function NodeRow({ node }: { node: OrchestrationNode }) {
  const [open, setOpen] = useState(false);
  const steps = node.steps ?? [];
  const toolCalls = toolCallsOf(node);
  const summary = node.output?.summary;
  const selfRun = node.preset === 'orchestrator';
  const running = node.status === 'running';
  const expandable = steps.length > 0 || Boolean(summary);
  const duration = spanMs(node.started_at, node.completed_at);

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={node.status} />
        <span className="font-medium text-fg">{node.node_id}</span>

        {/* WHO ran it. The orchestrator taking a step itself is a first-class outcome, not a gap. */}
        {selfRun ? (
          <Badge>orchestrator · no delegation</Badge>
        ) : node.assigned_agent_id ? (
          <span className="text-xs text-muted">
            → <AgentName agentId={node.assigned_agent_id} />
          </span>
        ) : node.preset ? (
          <Badge>{node.preset}</Badge>
        ) : null}

        <span className="ml-auto flex items-center gap-3 text-xs text-muted">
          {duration != null ? <span title="duration">{formatDuration(duration)}</span> : null}
          {node.tokens_used ? <span>{formatNumber(node.tokens_used)} tok</span> : null}
          {node.cost_usd ? <span>{formatCost(node.cost_usd)}</span> : null}
          {expandable ? (
            <button type="button" className="text-brand hover:underline" onClick={() => setOpen((v) => !v)}>
              {open ? 'Hide' : 'Details'}
            </button>
          ) : null}
        </span>
      </div>

      {/* The tools THIS sub-agent called — shown WITHOUT expanding, because "which tool did it use?"
          is the question people open this panel to answer. */}
      {toolCalls.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-faint">tools:</span>
          {toolCalls.map((s, i) => (
            <Badge key={`${s.tool ?? 'tool'}-${i}`} tone={s.error ? 'danger' : 'success'}>
              {s.tool ?? 'tool'}
            </Badge>
          ))}
        </div>
      ) : running ? (
        <p className="mt-1.5 text-[11px] text-faint">working…</p>
      ) : null}

      {node.depends_on.length > 0 ? (
        <p className="mt-1 text-[11px] text-faint">depends on: {node.depends_on.join(', ')}</p>
      ) : null}

      {open ? (
        <div className="mt-2.5 flex flex-col gap-2">
          {steps.length > 0 ? (
            <div className="rounded-md border border-border bg-surface-2 px-3 py-3">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-faint">
                What this agent did
              </p>
              {/* The exact timeline the Task Runner shows for a single agent — same component, so a
                  sub-agent's run reads identically to a standalone one. */}
              <TaskTimeline steps={steps} />
            </div>
          ) : null}

          {summary ? (
            <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">
                Summary returned to the orchestrator
              </p>
              <p className="whitespace-pre-wrap text-xs text-fg/90">{summary}</p>
              {node.output?.citations?.length ? (
                <p className="mt-1 text-[11px] text-faint">citations: {node.output.citations.join(', ')}</p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

// ── helpers ────────────────────────────────────────────────────────────────────────────
function toolCallsOf(node: OrchestrationNode): TaskStep[] {
  return (node.steps ?? []).filter((s) => s.step_type === 'tool_call');
}

function spanMs(from?: string | null, to?: string | null): number | null {
  if (!from || !to) return null;
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}

/**
 * Group nodes into topological WAVES — the driver's actual schedule (wave N runs only once every
 * wave < N is done; everything inside a wave runs concurrently).
 *
 * Kahn, but TOLERANT: this is display data, not a validated graph, so a dangling dependency or a
 * cycle that somehow reached the client must never hang the UI or silently swallow a node. Anything
 * still unplaced once the graph stops resolving is flushed as a final wave — a run always renders.
 */
function toWaves(nodes: OrchestrationNode[]): OrchestrationNode[][] {
  const ids = new Set(nodes.map((n) => n.node_id));
  const pending = new Map(nodes.map((n) => [n.node_id, n]));
  const placed = new Set<string>();
  const waves: OrchestrationNode[][] = [];

  while (pending.size > 0) {
    const ready = [...pending.values()].filter((n) =>
      // A dependency that is not in this payload cannot gate anything — ignore it rather than
      // deadlocking the whole render on it.
      n.depends_on.filter((d) => ids.has(d)).every((d) => placed.has(d)),
    );
    if (ready.length === 0) break; // cycle / unresolvable — flush the remainder below
    for (const n of ready) {
      pending.delete(n.node_id);
      placed.add(n.node_id);
    }
    waves.push(ready);
  }

  if (pending.size > 0) waves.push([...pending.values()]);
  return waves;
}

// Local Stat (accepts a ReactNode value) so the tiles can hold a StatusBadge / composed cost.
function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</p>
      <div className="mt-0.5 text-sm font-medium text-fg">{value}</div>
    </div>
  );
}
