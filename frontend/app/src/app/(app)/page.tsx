'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import type { ReactNode } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentName } from '@/components/AgentNames';
import { Pipeline, type PipelineStage, type StageState } from '@/components/Pipeline';
import { useSession } from '@/components/SessionProvider';
import { Button, Card, CardBody, CardHeader, Loading } from '@/components/ui';
import { bffFetch } from '@/lib/bff-client';
import { getCost, getTask, getUsage, listAgents, listTasks, listViolations } from '@/lib/services';
import type { CostRow, TaskListItem, TaskStatus, TaskStep, UsageRow, Violation } from '@/lib/types';
import { useAsync } from '@/lib/useAsync';
import { cn } from '@/lib/utils';

// ── formatting helpers ────────────────────────────────────────────────────────
const nf = new Intl.NumberFormat('en-US');
function fmtInt(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : nf.format(n);
}
function fmtTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
function fmtUsd(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : `$${n.toFixed(2)}`;
}
function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.round(h / 24)}d`;
}
function sumBy<T>(rows: T[], key: keyof T): number {
  return rows.reduce((acc, r) => acc + (Number(r[key]) || 0), 0);
}
function short(id: string | null | undefined): string {
  if (!id) return '—';
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-3)}` : id;
}

// ── task status → semantic dot + label ────────────────────────────────────────
const TASK_META: Record<TaskStatus, { label: string; dot: string }> = {
  pending: { label: 'Pending', dot: 'bg-muted' },
  running: { label: 'Running', dot: 'bg-brand' },
  completed: { label: 'Done', dot: 'bg-success' },
  failed: { label: 'Failed', dot: 'bg-danger' },
  cancelled: { label: 'Cancelled', dot: 'bg-muted' },
  timeout: { label: 'Timeout', dot: 'bg-danger' },
};

// ── task steps → pipeline stages ──────────────────────────────────────────────
const STEP_LABEL: Record<string, string> = {
  input: 'Guard In',
  pre_guardrail: 'Guard In',
  guard_in: 'Guard In',
  prompt_build: 'Prompt',
  prompt: 'Prompt',
  llm_call: 'LLM',
  llm: 'LLM',
  output: 'Guard Out',
  post_guardrail: 'Guard Out',
  guard_out: 'Guard Out',
  tool_call: 'Tools',
  tools: 'Tools',
  tool_loop: 'Tools',
  memory: 'Memory',
  memory_write: 'Memory',
};
const CANONICAL_IDLE: PipelineStage[] = ['Guard In', 'Prompt', 'LLM', 'Guard Out', 'Tools', 'Memory'].map((label) => ({
  label,
  state: 'idle' as StageState,
}));

function stepState(status: string): StageState {
  const s = (status ?? '').toLowerCase();
  if (['completed', 'ok', 'allow', 'success', 'done'].includes(s)) return 'done';
  if (['running', 'in_progress', 'active', 'started'].includes(s)) return 'active';
  if (['blocked', 'failed', 'error', 'timeout', 'block', 'denied'].includes(s)) return 'block';
  return 'idle';
}
function humanize(step: string): string {
  return step.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}
function mapSteps(steps: TaskStep[] | undefined): PipelineStage[] {
  if (!steps || steps.length === 0) return [];
  return steps.map((st) => ({
    label: STEP_LABEL[(st.step ?? '').toLowerCase()] ?? humanize(st.step ?? '—'),
    meta: st.duration_ms != null ? `${st.duration_ms}ms` : st.tokens != null ? `${st.tokens} tok` : undefined,
    state: stepState(st.status),
  }));
}

interface HealthResponse {
  services?: Record<string, { livez?: number | null; readyz?: number | null }>;
}
interface ServiceHealth {
  name: string;
  state: 'ready' | 'degraded' | 'down';
}
interface PipelineView {
  stages: PipelineStage[];
  task: { taskId: string; agentId: string; trace: string; status: TaskStatus } | null;
}
interface Overview {
  activeAgents: number | null;
  agentsCapped: boolean;
  tasks24h: number | null;
  tokens24h: number | null;
  spend24h: number | null;
  blocked24h: number | null;
  blockedCapped: boolean;
  decisions: { block: number; redact: number; warn: number } | null;
  recent: TaskListItem[] | null;
  health: ServiceHealth[] | null;
  pipeline: PipelineView;
}

function settledValue<T>(r: PromiseSettledResult<T>): T | null {
  return r.status === 'fulfilled' ? r.value : null;
}

async function loadOverview(signal: AbortSignal): Promise<Overview> {
  const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const [costR, usageR, agentsR, violR, tasksR, healthR] = await Promise.allSettled([
    getCost({ from: since, group_by: 'model' }, signal),
    getUsage({ from: since, group_by: 'model' }, signal),
    listAgents({ status: 'active', limit: 100 }, signal),
    listViolations({ from: since, limit: 100 }, signal),
    listTasks({ limit: 8 }, signal),
    bffFetch<HealthResponse>('/health', { signal }),
  ]);

  const cost = settledValue(costR);
  const usage = settledValue(usageR);
  const agents = settledValue(agentsR);
  const viol = settledValue(violR);
  const tasks = settledValue(tasksR);
  const health = settledValue(healthR);

  const costRows: CostRow[] = cost?.data ?? [];
  const usageRows: UsageRow[] = usage?.data ?? [];
  const agentItems = agents ? (agents.items ?? agents.agents ?? agents.data ?? []) : null;
  const violations: Violation[] | null = viol?.violations ?? null;

  const decisions =
    violations === null
      ? null
      : violations.reduce(
          (acc, v) => {
            const d = (v.decision ?? '').toLowerCase();
            if (d === 'block') acc.block += 1;
            else if (d === 'redact') acc.redact += 1;
            else if (d === 'warn') acc.warn += 1;
            return acc;
          },
          { block: 0, redact: 0, warn: 0 },
        );

  const services: ServiceHealth[] | null = health?.services
    ? Object.entries(health.services).map(([name, s]) => ({
        name,
        state: s?.readyz === 200 ? 'ready' : s?.livez === 200 ? 'degraded' : 'down',
      }))
    : null;

  // Latest task → its real execution pipeline (falls back to an idle rail).
  const recentTasks = tasks?.tasks ?? [];
  let pipeline: PipelineView = { stages: CANONICAL_IDLE, task: null };
  if (recentTasks.length) {
    const latest = recentTasks.find((t) => t.status === 'running') ?? recentTasks[0];
    const view: PipelineView = {
      stages: CANONICAL_IDLE,
      task: { taskId: latest.task_id, agentId: latest.agent_id, trace: latest.trace_id ?? latest.task_id, status: latest.status },
    };
    try {
      const detail = await getTask(latest.task_id, signal);
      if (view.task) {
        view.task.status = detail.status;
        view.task.trace = detail.trace_id ?? view.task.trace;
      }
      const mapped = mapSteps(detail.task_steps);
      if (mapped.length) view.stages = mapped;
    } catch {
      /* keep idle rail + the list-item header */
    }
    pipeline = view;
  }

  const tasks24h = usage ? sumBy(usageRows, 'request_count') : cost ? sumBy(costRows, 'request_count') : null;
  const tokens24h = usage ? sumBy(usageRows, 'total_tokens') : cost ? sumBy(costRows, 'total_tokens') : null;

  return {
    activeAgents: agentItems ? agentItems.length : null,
    agentsCapped: Boolean(agents?.next_cursor),
    tasks24h,
    tokens24h,
    spend24h: cost ? sumBy(costRows, 'cost_usd') : null,
    blocked24h: decisions ? decisions.block : null,
    blockedCapped: Boolean(viol?.has_more),
    decisions,
    recent: tasks ? tasks.tasks : null,
    health: services,
    pipeline,
  };
}

// ── small presentational pieces ───────────────────────────────────────────────
function Metric({ label, value, sub, alert }: { label: string; value: ReactNode; sub?: ReactNode; alert?: boolean }) {
  return (
    <div className="border-l border-border px-3.5 py-3 first:border-l-0">
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className={cn('mt-1 text-[22px] font-semibold tabular-nums', alert ? 'text-danger' : 'text-fg-strong')}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
    </div>
  );
}

function StatusChip({ status }: { status: TaskStatus }) {
  const meta = TASK_META[status] ?? { label: status, dot: 'bg-muted' };
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-border-2 px-2 py-0.5 text-xs font-medium text-fg">
      <span className={cn('h-[7px] w-[7px] rounded-full', meta.dot)} />
      {meta.label}
    </span>
  );
}

function DecisionRow({ label, count, total, color }: { label: string; count: number; total: number; color: string }) {
  const pct = total > 0 ? (count / total) * 100 : 0;
  return (
    <div className="flex items-center gap-2 border-t border-border py-1.5 text-[13px] first:border-0">
      <span className={cn('h-2 w-2 rounded-sm', color)} />
      <span className="text-muted">{label}</span>
      <span className="ml-auto font-mono tabular-nums text-fg">{fmtInt(count)}</span>
      <span className="w-11 text-right font-mono text-xs text-faint">{pct.toFixed(1)}%</span>
    </div>
  );
}

export default function OverviewPage() {
  const { session } = useSession();
  const router = useRouter();
  const { data, loading, reload } = useAsync(loadOverview, []);

  const tenant = session?.tenant_id ?? '—';

  return (
    <Page>
      <PageHeader
        title="Overview"
        description={`${tenant} · prod`}
        actions={
          <>
            <Button variant="secondary" size="md" onClick={() => reload()}>
              Refresh
            </Button>
            <Link href="/tasks/run">
              <Button variant="secondary" size="md">
                Run Task
              </Button>
            </Link>
            <Link href="/agents">
              <Button variant="primary" size="md">
                New Agent
              </Button>
            </Link>
          </>
        }
      />
      <PageBody>

      {loading && !data ? (
        <Loading label="Loading overview…" />
      ) : (
        <div className="flex flex-col gap-3">
          {/* live pipeline — the agent execution rail for the latest task */}
          <Card>
            <CardHeader
              title="Live Pipeline"
              actions={
                data?.pipeline.task ? (
                  <div className="flex items-center gap-2.5">
                    <AgentName agentId={data.pipeline.task.agentId} className="text-xs text-fg" />
                    <span className="font-mono text-xs text-muted">{short(data.pipeline.task.trace)}</span>
                    <StatusChip status={data.pipeline.task.status} />
                  </div>
                ) : (
                  <span className="text-[13px] text-muted">No Recent Tasks</span>
                )
              }
            />
            <CardBody>
              <Pipeline stages={data?.pipeline.stages ?? CANONICAL_IDLE} className="px-1 py-1" />
            </CardBody>
          </Card>

          {/* metrics */}
          <div className="grid grid-cols-2 overflow-hidden rounded-md border border-border bg-surface sm:grid-cols-4">
            <Metric
              label="Active Agents"
              value={data?.activeAgents === null ? '—' : `${fmtInt(data?.activeAgents)}${data?.agentsCapped ? '+' : ''}`}
            />
            <Metric label="Tasks · 24h" value={fmtInt(data?.tasks24h)} sub={`${fmtTokens(data?.tokens24h)} Tokens`} />
            <Metric
              label="Blocked · 24h"
              value={data?.blocked24h === null ? '—' : `${fmtInt(data?.blocked24h)}${data?.blockedCapped ? '+' : ''}`}
              sub="Guardrail Blocks"
              alert={Boolean(data?.blocked24h && data.blocked24h > 0)}
            />
            <Metric label="Spend · 24h" value={fmtUsd(data?.spend24h)} />
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1.6fr_1fr]">
            {/* recent tasks */}
            <Card>
              <CardHeader
                title="Recent Tasks"
                actions={
                  <Link href="/tasks" className="text-[13px] font-medium text-brand hover:underline">
                    View All
                  </Link>
                }
              />
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="text-left text-xs text-muted">
                      <th className="border-b border-border px-3 py-2.5 font-medium">Trace</th>
                      <th className="border-b border-border px-3 py-2.5 font-medium">Agent</th>
                      <th className="border-b border-border px-3 py-2.5 font-medium">Status</th>
                      <th className="border-b border-border px-3 py-2.5 text-right font-medium">Tokens</th>
                      <th className="border-b border-border px-3 py-2.5 text-right font-medium">Cost</th>
                      <th className="border-b border-border px-3 py-2.5 text-right font-medium">Age</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!data?.recent ? (
                      <tr>
                        <td colSpan={6} className="px-3 py-9 text-center text-sm text-muted">
                          Task feed unavailable.
                        </td>
                      </tr>
                    ) : data.recent.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="px-3 py-9 text-center text-sm text-muted">
                          No tasks yet. Run one from the Task Runner.
                        </td>
                      </tr>
                    ) : (
                      data.recent.map((t) => {
                        const meta = TASK_META[t.status] ?? { label: t.status, dot: 'bg-muted' };
                        return (
                          <tr
                            key={t.task_id}
                            onClick={() => router.push(`/tasks/${t.task_id}`)}
                            className="cursor-pointer border-b border-border transition-colors last:border-0 hover:bg-surface-2"
                          >
                            <td className="px-3 py-2.5 font-mono text-xs text-muted">{short(t.trace_id ?? t.task_id)}</td>
                            <td className="px-3 py-2.5 text-xs text-fg"><AgentName agentId={t.agent_id} /></td>
                            <td className="px-3 py-2.5">
                              <span className="inline-flex items-center gap-1.5">
                                <span className={cn('h-[7px] w-[7px] rounded-full', meta.dot)} />
                                {meta.label}
                              </span>
                            </td>
                            <td className="px-3 py-2.5 text-right font-mono tabular-nums text-fg">{fmtInt(t.tokens_used ?? 0)}</td>
                            <td className="px-3 py-2.5 text-right font-mono tabular-nums text-fg">{fmtUsd(t.cost_usd ?? 0)}</td>
                            <td className="px-3 py-2.5 text-right font-mono text-muted">{timeAgo(t.created_at)}</td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </Card>

            {/* guardrail activity */}
            <Card>
              <CardHeader title="Guardrail Activity" description="Non-Allow Decisions · 24h" />
              <CardBody>
                {!data?.decisions ? (
                  <p className="py-6 text-center text-sm text-muted">Guardrail activity unavailable.</p>
                ) : data.decisions.block + data.decisions.redact + data.decisions.warn === 0 ? (
                  <p className="py-6 text-center text-sm text-muted">No violations in the last 24h.</p>
                ) : (
                  (() => {
                    const total = data.decisions.block + data.decisions.redact + data.decisions.warn;
                    return (
                      <div className="flex flex-col gap-3">
                        <div className="flex h-2 overflow-hidden rounded-sm bg-surface-2">
                          <span className="bg-danger" style={{ width: `${(data.decisions.block / total) * 100}%` }} />
                          <span className="bg-info" style={{ width: `${(data.decisions.redact / total) * 100}%` }} />
                          <span className="bg-warning" style={{ width: `${(data.decisions.warn / total) * 100}%` }} />
                        </div>
                        <div>
                          <DecisionRow label="Blocked" count={data.decisions.block} total={total} color="bg-danger" />
                          <DecisionRow label="Redacted" count={data.decisions.redact} total={total} color="bg-info" />
                          <DecisionRow label="Warned" count={data.decisions.warn} total={total} color="bg-warning" />
                        </div>
                        <Link href="/guardrails" className="text-[13px] font-medium text-brand hover:underline">
                          Review Violations
                        </Link>
                      </div>
                    );
                  })()
                )}
              </CardBody>
            </Card>
          </div>

          {/* platform health */}
          <Card>
            <CardHeader
              title="Platform Health"
              actions={
                <Link href="/health" className="text-[13px] font-medium text-brand hover:underline">
                  Details
                </Link>
              }
            />
            {!data?.health ? (
              <CardBody>
                <p className="text-center text-sm text-muted">Health unavailable.</p>
              </CardBody>
            ) : (
              <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">
                {data.health.map((s) => (
                  <div key={s.name} className="flex items-center gap-2 bg-surface px-3 py-2.5 text-[13px]">
                    <span
                      className={cn(
                        'h-[7px] w-[7px] rounded-full',
                        s.state === 'ready' ? 'bg-success' : s.state === 'degraded' ? 'bg-warning' : 'bg-danger',
                      )}
                    />
                    <span className="font-mono text-[13px] text-fg">{s.name}</span>
                    <span className="ml-auto text-xs text-muted">
                      {s.state === 'ready' ? 'Ready' : s.state === 'degraded' ? 'Degraded' : 'Down'}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}
      </PageBody>
    </Page>
  );
}
