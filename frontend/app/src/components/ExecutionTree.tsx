'use client';

import { useEffect, useState } from 'react';
import { Badge, Card, CardBody, CardHeader, StatusBadge } from '@/components/ui';
import { AgentName } from '@/components/AgentNames';
import { getTask, type OrchestrationGraph, type OrchestrationNode } from '@/lib/services';
import type { TaskStep } from '@/lib/types';
import { formatCost, formatNumber } from '@/lib/utils';

/**
 * The live execution tree for an orchestration run: the aggregate run stats + one row per DAG node
 * (sub-agent), showing status, the assigned sub-agent, tokens/cost, its dependencies, and an
 * expandable summary. Fed by the SSE `run` frames (or a one-shot graph fetch). "Reads like Claude":
 * the orchestrator delegates and you watch each sub-agent's node light up — never the raw transcript.
 */
export function ExecutionTree({ graph }: { graph: OrchestrationGraph | null }) {
  if (!graph) {
    return <p className="py-8 text-center text-sm text-muted">Submit a goal to watch the orchestration.</p>;
  }
  const { workflow, nodes } = graph;
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Status" value={<StatusBadge status={workflow.status} />} />
        <Stat label="Sub-agents" value={String(nodes.length)} />
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
        <CardHeader title="Execution Tree" description={workflow.decomposition ? `decomposed via ${workflow.decomposition}` : undefined} />
        <CardBody className="p-0">
          {nodes.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-muted">Planning…</p>
          ) : (
            <ul className="divide-y divide-border">
              {nodes.map((n) => (
                <NodeRow key={n.node_id} node={n} />
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      {workflow.output?.message ? (
        <Card>
          <CardHeader title="Final Answer" />
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

function NodeRow({ node }: { node: OrchestrationNode }) {
  const [open, setOpen] = useState(false);
  const [steps, setSteps] = useState<TaskStep[] | null>(null);
  const [stepsError, setStepsError] = useState(false);
  const summary = node.output?.summary;
  const expandable = Boolean(summary || node.task_id);

  // Lazily pull the sub-agent's audit trail on expand (never on initial render — that would be an
  // N+1 across every node). The tool calls live on the node's OWN task, so this is what makes
  // "which tools did THIS sub-agent actually use" visible per agent.
  useEffect(() => {
    if (!open || !node.task_id || steps !== null || stepsError) return;
    let cancelled = false;
    void (async () => {
      try {
        const task = await getTask(node.task_id as string);
        if (!cancelled) setSteps(task.task_steps ?? []);
      } catch {
        if (!cancelled) setStepsError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, node.task_id, steps, stepsError]);

  const toolCalls = (steps ?? []).filter((s) => s.step_type === 'tool_call');

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={node.status} />
        <span className="font-medium text-fg">{node.node_id}</span>
        {node.preset ? <Badge>{node.preset}</Badge> : null}
        {node.assigned_agent_id ? (
          <span className="text-xs text-muted">
            → <AgentName agentId={node.assigned_agent_id} />
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-3 text-xs text-muted">
          {node.tokens_used ? <span>{formatNumber(node.tokens_used)} tok</span> : null}
          {node.cost_usd ? <span>{formatCost(node.cost_usd)}</span> : null}
          {expandable ? (
            <button type="button" className="text-brand hover:underline" onClick={() => setOpen((v) => !v)}>
              {open ? 'Hide' : 'Details'}
            </button>
          ) : null}
        </span>
      </div>
      {node.depends_on.length > 0 ? (
        <p className="mt-1 text-[11px] text-faint">depends on: {node.depends_on.join(', ')}</p>
      ) : null}

      {open ? (
        <div className="mt-2 flex flex-col gap-2">
          {summary ? (
            <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Summary</p>
              <p className="whitespace-pre-wrap text-xs text-fg/90">{summary}</p>
              {node.output?.citations?.length ? (
                <p className="mt-1 text-[11px] text-faint">citations: {node.output.citations.join(', ')}</p>
              ) : null}
            </div>
          ) : null}

          {node.task_id ? (
            <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">
                Tool Calls
              </p>
              {stepsError ? (
                <p className="text-xs text-muted">Could not load this sub-agent&apos;s trace.</p>
              ) : steps === null ? (
                <p className="text-xs text-muted">Loading…</p>
              ) : toolCalls.length === 0 ? (
                <p className="text-xs text-muted">
                  No tools called — this sub-agent answered from the model alone.
                </p>
              ) : (
                <ul className="flex flex-col gap-1">
                  {toolCalls.map((s, i) => (
                    <li key={`${s.tool ?? 'tool'}-${i}`} className="flex flex-wrap items-center gap-2">
                      <Badge tone={s.error ? 'danger' : 'success'}>{s.tool ?? 'tool'}</Badge>
                      {s.error ? <span className="text-[11px] text-danger">{s.error}</span> : null}
                      <span className="ml-auto text-[11px] text-faint">
                        {s.duration_ms != null ? `${formatNumber(s.duration_ms)} ms` : null}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
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
