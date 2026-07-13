'use client';

import { useMemo, useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentName, useAgentNames } from '@/components/AgentNames';
import { BarChart } from '@/components/BarChart';
import type { BarDatum } from '@/components/BarChart';
import { Card, CardBody, CardHeader, CopyButton, ErrorBanner, Loading, Select, Stat, Table } from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { getCost, getUsage } from '@/lib/services';
import type { CostRow, UsageRow } from '@/lib/types';
import { formatCost, formatNumber } from '@/lib/utils';

// Values MUST match the backend group_by allowlist: model | agent | api_key | date.
const GROUP_BY = ['date', 'model', 'agent', 'api_key'];
const GROUP_LABEL: Record<string, string> = { date: 'Date', model: 'Model', agent: 'Agent', api_key: 'API Key' };

function num(v: unknown): number {
  return typeof v === 'number' ? v : Number(v) || 0;
}

function groupLabel(row: Record<string, unknown>, groupBy: string): string {
  const v = row[groupBy];
  return v == null ? '(none)' : String(v);
}

/** Group-key table cell: agent id → name (never the UUID), api_key id → copy affordance, else plain text. */
function GroupCell({ row, groupBy }: { row: Record<string, unknown>; groupBy: string }) {
  if (groupBy === 'agent') {
    return <AgentName agentId={String(row.agent ?? row.agent_id ?? '')} />;
  }
  if (groupBy === 'api_key') {
    const keyId = String(row.api_key ?? row.api_key_id ?? '');
    return keyId ? <CopyButton value={keyId} label="Copy Key ID" /> : <span className="text-fg">(none)</span>;
  }
  return <span className="text-fg">{groupLabel(row, groupBy)}</span>;
}

function LegendDot({ tone, label }: { tone: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span className={`h-2 w-2 rounded-sm ${tone}`} />
      {label}
    </span>
  );
}

export default function UsagePage() {
  const [groupBy, setGroupBy] = useState('model');
  const usageQ = useAsync((signal) => getUsage({ group_by: groupBy }, signal), [groupBy]);
  const costQ = useAsync((signal) => getCost({ group_by: groupBy }, signal), [groupBy]);
  const { nameOf } = useAgentNames();

  // Chart labels are plain strings: resolve an agent group key to its NAME (never the UUID); others as-is.
  const barLabel = (row: Record<string, unknown>): string => {
    if (groupBy === 'agent') {
      const id = String(row.agent ?? row.agent_id ?? '');
      return nameOf(id) ?? (id ? '(unnamed agent)' : '(none)');
    }
    return groupLabel(row, groupBy);
  };

  // Memoize the rows off the stable query-data references (not freshly-allocated arrays).
  const usage = useMemo(() => usageQ.data?.data ?? [], [usageQ.data]);
  const cost = useMemo(() => costQ.data?.data ?? [], [costQ.data]);

  const totals = useMemo(() => {
    let tokens = 0;
    let requests = 0;
    let cacheRead = 0;
    let cacheWrite = 0;
    for (const r of usage) {
      tokens += num(r.total_tokens) || num(r.prompt_tokens) + num(r.completion_tokens);
      requests += num(r.request_count);
      cacheRead += num(r.cache_read_tokens);
      cacheWrite += num(r.cache_write_tokens);
    }
    let costSum = 0;
    for (const c of cost) costSum += num(c.cost_usd);
    return { tokens, requests, cacheRead, cacheWrite, costSum };
  }, [usage, cost]);

  // Token bars with a cache-read/write/fresh stacked breakdown per group.
  const tokenBars: BarDatum[] = usage.map((r) => {
    const total = num(r.total_tokens) || num(r.prompt_tokens) + num(r.completion_tokens);
    const cacheRead = num(r.cache_read_tokens);
    const cacheWrite = num(r.cache_write_tokens);
    const fresh = Math.max(0, total - cacheRead - cacheWrite);
    return {
      label: barLabel(r),
      value: total,
      segments: [
        { label: 'Fresh', value: fresh, tone: 'bg-brand' },
        { label: 'Cache Write', value: cacheWrite, tone: 'bg-warning' },
        { label: 'Cache Read', value: cacheRead, tone: 'bg-success' },
      ],
    };
  });

  const costBars: BarDatum[] = cost.map((c) => ({ label: barLabel(c), value: num(c.cost_usd) }));

  const gh = GROUP_LABEL[groupBy] ?? groupBy;
  const usageColumns: Array<Column<UsageRow>> = [
    { key: 'group', header: gh, render: (r) => <GroupCell row={r} groupBy={groupBy} /> },
    { key: 'prompt', header: 'Prompt', className: 'text-right', render: (r) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(r.prompt_tokens))}</span> },
    { key: 'completion', header: 'Completion', className: 'text-right', render: (r) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(r.completion_tokens))}</span> },
    { key: 'cache_read', header: 'Cache Read', className: 'text-right', render: (r) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(r.cache_read_tokens))}</span> },
    { key: 'cache_write', header: 'Cache Write', className: 'text-right', render: (r) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(r.cache_write_tokens))}</span> },
    { key: 'total', header: 'Total', className: 'text-right', render: (r) => <span className="font-mono text-xs tabular-nums text-fg">{formatNumber(num(r.total_tokens) || num(r.prompt_tokens) + num(r.completion_tokens))}</span> },
    { key: 'requests', header: 'Requests', className: 'text-right', render: (r) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(r.request_count))}</span> },
  ];

  const costColumns: Array<Column<CostRow>> = [
    { key: 'group', header: gh, render: (c) => <GroupCell row={c} groupBy={groupBy} /> },
    { key: 'cost', header: 'Cost (USD)', className: 'text-right', render: (c) => <span className="font-mono tabular-nums text-fg">{formatCost(num(c.cost_usd))}</span> },
    { key: 'tokens', header: 'Tokens', className: 'text-right', render: (c) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(c.total_tokens))}</span> },
    { key: 'requests', header: 'Requests', className: 'text-right', render: (c) => <span className="font-mono text-xs tabular-nums">{formatNumber(num(c.request_count))}</span> },
  ];

  return (
    <Page>
      <PageHeader
        title="LLM Usage & Cost"
        description="Token usage and cost from the LLMs gateway, grouped however you like."
        actions={
          <Select value={groupBy} onChange={(e) => setGroupBy(e.target.value)} className="w-44">
            {GROUP_BY.map((g) => (
              <option key={g} value={g}>
                Group by {GROUP_LABEL[g] ?? g}
              </option>
            ))}
          </Select>
        }
      />
      <PageBody>
        <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Total Tokens" value={formatNumber(totals.tokens)} />
          <Stat label="Total Cost" value={formatCost(totals.costSum)} />
          <Stat label="Requests" value={formatNumber(totals.requests)} />
          <Stat label="Cache Read Tokens" value={formatNumber(totals.cacheRead)} sub={`${formatNumber(totals.cacheWrite)} Written`} />
        </div>

        {usageQ.error ? <ErrorBanner error={usageQ.error} title="Could not load usage" className="mb-3" /> : null}
        {costQ.error ? <ErrorBanner error={costQ.error} title="Could not load cost" className="mb-3" /> : null}

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Card>
            <CardHeader
              title="Tokens by Group"
              actions={
                <div className="flex items-center gap-3">
                  <LegendDot tone="bg-brand" label="Fresh" />
                  <LegendDot tone="bg-warning" label="Cache Write" />
                  <LegendDot tone="bg-success" label="Cache Read" />
                </div>
              }
            />
            <CardBody>
              {usageQ.loading ? <Loading /> : <BarChart data={tokenBars} valueFormat={(v) => formatNumber(v)} />}
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Cost by Group" />
            <CardBody>{costQ.loading ? <Loading /> : <BarChart data={costBars} valueFormat={(v) => formatCost(v)} />}</CardBody>
          </Card>
        </div>

        <Card className="mt-3">
          <CardHeader title="Usage Detail" />
          <CardBody className="px-0 py-0">
            {usageQ.loading ? (
              <Loading />
            ) : (
              <Table columns={usageColumns} rows={usage} rowKey={(_, i) => String(i)} empty="No usage in this window." />
            )}
          </CardBody>
        </Card>

        <Card className="mt-3">
          <CardHeader title="Cost Detail" />
          <CardBody className="px-0 py-0">
            {costQ.loading ? (
              <Loading />
            ) : (
              <Table columns={costColumns} rows={cost} rowKey={(_, i) => String(i)} empty="No cost in this window." />
            )}
          </CardBody>
        </Card>
      </PageBody>
    </Page>
  );
}
