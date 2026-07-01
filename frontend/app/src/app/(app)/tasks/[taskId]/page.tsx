'use client';

import Link from 'next/link';
import { use } from 'react';
import { PageHeader } from '@/components/AppShell';
import { TaskTimeline } from '@/components/TaskTimeline';
import { Badge, Card, CardBody, CardHeader, ErrorBanner, Loading, Stat, StatusBadge } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { getTask } from '@/lib/services';
import { formatCost, formatDuration, formatNumber, formatTime } from '@/lib/utils';

export default function TaskDetailPage({ params }: { params: Promise<{ taskId: string }> }) {
  const { taskId } = use(params);
  const { data: task, loading, error } = useAsync((signal) => getTask(taskId, signal), [taskId]);

  return (
    <div>
      <PageHeader
        title="Task detail"
        description={<span className="font-mono text-xs">{taskId}</span>}
        actions={
          <Link href="/tasks" className="text-sm text-brand hover:underline">
            ← Task feed
          </Link>
        }
      />

      {error ? (
        <ErrorBanner error={error} title="Could not load this task" />
      ) : loading ? (
        <Loading label="Loading task…" />
      ) : task ? (
        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader
              title="Summary"
              actions={<StatusBadge status={task.status} />}
            />
            <CardBody>
              <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label="Tokens" value={formatNumber(task.tokens_used)} />
                <Stat label="Cost" value={formatCost(task.cost_usd)} />
                <Stat label="Duration" value={formatDuration(task.duration_ms ?? null)} />
                <Stat label="Steps" value={task.task_steps.length} />
              </div>
              <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                <Field label="Started" value={formatTime(task.started_at)} />
                <Field label="Completed" value={formatTime(task.completed_at)} />
                <Field label="Trace" value={<span className="font-mono text-xs">{task.trace_id ?? '—'}</span>} />
                <Field label="Test run" value={task.metadata?.test ? <Badge tone="info">test</Badge> : '—'} />
              </dl>

              {task.error ? (
                <div className="mt-4 rounded-md border border-danger/40 bg-danger/10 px-4 py-3" role="alert">
                  <div className="flex items-center gap-2">
                    <Badge tone="danger">{task.error.code}</Badge>
                    <span className="text-sm font-semibold text-fg">Terminal error</span>
                  </div>
                  <p className="mt-1 text-sm text-fg/90">{task.error.message}</p>
                  {task.error.trace_id && <p className="mt-1 font-mono text-xs text-muted">trace: {task.error.trace_id}</p>}
                </div>
              ) : null}

              {task.output?.message ? (
                <div className="mt-4 rounded-md border border-border bg-surface-2 px-4 py-3">
                  <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">Answer</p>
                  <p className="whitespace-pre-wrap text-sm text-fg">{task.output.message}</p>
                </div>
              ) : null}
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Timeline" description="Ordered stages: guardrail-in → llm → guardrail-out." />
            <CardBody>
              <TaskTimeline steps={task.task_steps} />
            </CardBody>
          </Card>
        </div>
      ) : null}
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p className="mt-1 text-fg">{value}</p>
    </div>
  );
}
