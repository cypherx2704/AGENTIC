'use client';

import { useMemo, useState } from 'react';
import { PageHeader } from '@/components/AppShell';
import { BarChart } from '@/components/BarChart';
import type { BarDatum } from '@/components/BarChart';
import { Card, CardBody, CardHeader, ErrorBanner, Loading, Select, Stat, Table } from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { getCost, getUsage } from '@/lib/services';
import type { CostRow, UsageRow } from '@/lib/types';
import { formatCost, formatNumber } from '@/lib/utils';

const GROUP_BY = ['date', 'model', 'agent_id', 'provider'];

function num(v: unknown): number {
  return typeof v === 'number' ? v : Number(v) || 0;
}

function groupLabel(row: Record<string, unknown>, groupBy: string): string {
  const v = row[groupBy];
  return v == null ? '(none)' : String(v);
}

export default function UsagePage() {
  const [groupBy, setGroupBy] = useState('model');
  const usageQ = useAsync((signal) => getUsage({ group_by: groupBy }, signal), [groupBy]);
  const costQ = useAsync((signal) => getCost({ group_by: groupBy }, signal), [groupBy]);

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
      label: groupLabel(r, groupBy),
      value: total,
      segments: [
        { label: 'fresh', value: fresh, tone: 'bg-brand' },
        { label: 'cache write', value: cacheWrite, tone: 'bg-warning' },
        { label: 'cache read', value: cacheRead, tone: 'bg-success' },
      ],
    };
  });

  const costBars: BarDatum[] = cost.map((c) => ({ label: groupLabel(c, groupBy), value: num(c.cost_usd) }));

  const usageColumns: Array<Column<UsageRow>> = [
    { key: 'group', header: groupBy, render: (r) => <span className="text-fg">{groupLabel(r, groupBy)}</span> },
    { key: 'prompt', header: 'Prompt', render: (r) => formatNumber(num(r.prompt_tokens)) },
    { key: 'completion', header: 'Completion', render: (r) => formatNumber(num(r.completion_tokens)) },
    { key: 'cache_read', header: 'Cache read', render: (r) => formatNumber(num(r.cache_read_tokens)) },
    { key: 'cache_write', header: 'Cache write', render: (r) => formatNumber(num(r.cache_write_tokens)) },
    { key: 'total', header: 'Total', render: (r) => formatNumber(num(r.total_tokens) || num(r.prompt_tokens) + num(r.completion_tokens)) },
    { key: 'requests', header: 'Requests', render: (r) => formatNumber(num(r.request_count)) },
  ];

  const costColumns: Array<Column<CostRow>> = [
    { key: 'group', header: groupBy, render: (c) => <span className="text-fg">{groupLabel(c, groupBy)}</span> },
    { key: 'cost', header: 'Cost (USD)', render: (c) => <span className="font-mono">{formatCost(num(c.cost_usd))}</span> },
    { key: 'tokens', header: 'Tokens', render: (c) => formatNumber(num(c.total_tokens)) },
    { key: 'requests', header: 'Requests', render: (c) => formatNumber(num(c.request_count)) },
  ];

  return (
    <div>
      <PageHeader
        title="LLM usage & cost"
        description="Token usage and cost from the LLMs gateway, grouped however you like."
        actions={
          <Select value={groupBy} onChange={(e) => setGroupBy(e.target.value)} className="w-40">
            {GROUP_BY.map((g) => (
              <option key={g} value={g}>
                group by {g}
              </option>
            ))}
          </Select>
        }
      />

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Total tokens" value={formatNumber(totals.tokens)} />
        <Stat label="Total cost" value={formatCost(totals.costSum)} />
        <Stat label="Requests" value={formatNumber(totals.requests)} />
        <Stat label="Cache read tokens" value={formatNumber(totals.cacheRead)} sub={`${formatNumber(totals.cacheWrite)} written`} />
      </div>

      {usageQ.error ? <ErrorBanner error={usageQ.error} title="Could not load usage" className="mb-4" /> : null}
      {costQ.error ? <ErrorBanner error={costQ.error} title="Could not load cost" className="mb-4" /> : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Tokens by group" description="Stacked: fresh / cache write / cache read." />
          <CardBody>
            {usageQ.loading ? <Loading /> : <BarChart data={tokenBars} valueFormat={(v) => formatNumber(v)} />}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Cost by group" />
          <CardBody>{costQ.loading ? <Loading /> : <BarChart data={costBars} valueFormat={(v) => formatCost(v)} />}</CardBody>
        </Card>
      </div>

      <Card className="mt-6">
        <CardHeader title="Usage detail" />
        <CardBody className="px-0 py-0">
          {usageQ.loading ? (
            <Loading />
          ) : (
            <Table columns={usageColumns} rows={usage} rowKey={(_, i) => String(i)} empty="No usage in this window." />
          )}
        </CardBody>
      </Card>

      <Card className="mt-6">
        <CardHeader title="Cost detail" />
        <CardBody className="px-0 py-0">
          {costQ.loading ? (
            <Loading />
          ) : (
            <Table columns={costColumns} rows={cost} rowKey={(_, i) => String(i)} empty="No cost in this window." />
          )}
        </CardBody>
      </Card>
    </div>
  );
}
