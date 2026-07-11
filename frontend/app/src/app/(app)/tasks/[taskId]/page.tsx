'use client';

import Link from 'next/link';
import { use, useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { Pipeline } from '@/components/Pipeline';
import { TaskTimeline } from '@/components/TaskTimeline';
import { Badge, Button, Card, CardBody, CardHeader, ConfirmDialog, CopyButton, ErrorBanner, Loading, Stat, StatusBadge, useToast } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { BffError } from '@/lib/bff-client';
import { cancelTask, getTask } from '@/lib/services';
import { stepsToStages } from '@/lib/pipeline';
import { formatCost, formatDuration, formatNumber, formatTime } from '@/lib/utils';

export default function TaskDetailPage({ params }: { params: Promise<{ taskId: string }> }) {
  const { taskId } = use(params);
  const toast = useToast();
  const { data: task, loading, error, reload } = useAsync((signal) => getTask(taskId, signal), [taskId]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const stages = task ? stepsToStages(task.task_steps) : [];
  const canCancel = !!task && (task.status === 'pending' || task.status === 'running');

  async function onCancel() {
    setCancelling(true);
    try {
      await cancelTask(taskId);
      toast.success('Task cancelled.');
      setConfirmOpen(false);
      reload();
    } catch (err) {
      if (err instanceof BffError && err.status === 409) {
        toast.info('Task already finished.');
        setConfirmOpen(false);
        reload();
      } else {
        toast.error(err instanceof Error ? err.message : 'Could not cancel this task.');
      }
    } finally {
      setCancelling(false);
    }
  }

  return (
    <Page>
      <PageHeader
        title="Task Detail"
        description={<CopyButton value={taskId} label="Copy Task ID" />}
        actions={
          <>
            {task && <StatusBadge status={task.status} />}
            {canCancel && (
              <Button variant="danger" size="md" onClick={() => setConfirmOpen(true)}>
                Cancel Task
              </Button>
            )}
            <Link href="/tasks" className="text-[13px] font-medium text-brand hover:underline">
              ← Task Feed
            </Link>
          </>
        }
      />

      <PageBody>
      {error ? (
        <ErrorBanner error={error} title="Could not load this task" />
      ) : loading ? (
        <Loading label="Loading task…" />
      ) : task ? (
        <div className="flex flex-col gap-3">
          {stages.length > 0 && (
            <Card>
              <CardHeader title="Execution Pipeline" description="The stages this task ran through, in order." />
              <CardBody>
                <Pipeline stages={stages} className="px-1 py-1" />
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader title="Summary" actions={<StatusBadge status={task.status} />} />
            <CardBody>
              <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label="Tokens" value={formatNumber(task.tokens_used)} />
                <Stat label="Cost" value={formatCost(task.cost_usd)} />
                <Stat label="Duration" value={formatDuration(task.duration_ms ?? null)} />
                <Stat label="Steps" value={task.task_steps.length} />
              </div>
              <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
                <Field label="Started" value={formatTime(task.started_at)} />
                <Field label="Completed" value={formatTime(task.completed_at)} />
                <Field label="Trace" value={<span className="font-mono text-xs">{task.trace_id ?? '—'}</span>} />
                <Field label="Test Run" value={task.metadata?.test ? <Badge tone="info">Test</Badge> : '—'} />
              </dl>

              {task.error ? (
                <div className="mt-4 rounded-md border border-danger/40 bg-danger/10 px-4 py-3" role="alert">
                  <div className="flex items-center gap-2">
                    <Badge tone="danger">{task.error.code}</Badge>
                    <span className="text-sm font-semibold text-fg">Terminal Error</span>
                  </div>
                  <p className="mt-1 text-sm text-fg/90">{task.error.message}</p>
                  {task.error.trace_id && <p className="mt-1 font-mono text-xs text-muted">trace: {task.error.trace_id}</p>}
                </div>
              ) : null}

              {task.output?.message ? (
                <div className="mt-4 rounded-md border border-border bg-surface-2 px-4 py-3">
                  <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Answer</p>
                  <p className="whitespace-pre-wrap text-sm text-fg">{task.output.message}</p>
                </div>
              ) : null}
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Timeline" description="Ordered stages with per-step status, duration, and tokens." />
            <CardBody>
              <TaskTimeline steps={task.task_steps} />
            </CardBody>
          </Card>
        </div>
      ) : null}
      </PageBody>

      <ConfirmDialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        onConfirm={() => void onCancel()}
        title="Cancel This Task?"
        description="This stops the run where it is."
        confirmLabel="Cancel Task"
        cancelLabel="Keep Running"
        loading={cancelling}
      >
        <p className="text-sm text-muted">
          The task will stop executing and be marked cancelled. Work already in progress cannot be resumed.
        </p>
      </ConfirmDialog>
    </Page>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</p>
      <p className="mt-1 text-fg">{value}</p>
    </div>
  );
}
