'use client';

import { useState } from 'react';
import { Badge, Card, CardBody, CardHeader, StatusBadge } from '@/components/ui';
import { AgentName } from '@/components/AgentNames';
import type { OrchestrationGraph, OrchestrationNode } from '@/lib/services';
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
  const summary = node.output?.summary;
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
          {summary ? (
            <button type="button" className="text-brand hover:underline" onClick={() => setOpen((v) => !v)}>
              {open ? 'Hide' : 'Summary'}
            </button>
          ) : null}
        </span>
      </div>
      {node.depends_on.length > 0 ? (
        <p className="mt-1 text-[11px] text-faint">depends on: {node.depends_on.join(', ')}</p>
      ) : null}
      {open && summary ? (
        <div className="mt-2 rounded-md border border-border bg-surface-2 px-3 py-2">
          <p className="whitespace-pre-wrap text-xs text-fg/90">{summary}</p>
          {node.output?.citations?.length ? (
            <p className="mt-1 text-[11px] text-faint">citations: {node.output.citations.join(', ')}</p>
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
