'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentName } from '@/components/AgentNames';
import { useAgentList } from '@/lib/useAgentList';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  ErrorBanner,
  Input,
  Loading,
  Select,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import { cancelTask, listTasks } from '@/lib/services';
import type { TaskListItem } from '@/lib/types';
import { config } from '@/lib/config';
import { cn } from '@/lib/utils';
import { formatCost, formatNumber, formatTime } from '@/lib/utils';

const STATUSES = ['', 'pending', 'running', 'completed', 'failed', 'cancelled', 'timeout'];
const STATUS_LABEL: Record<string, string> = {
  '': 'All',
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  timeout: 'Timeout',
};

export default function TaskFeedPage() {
  const router = useRouter();
  const { agents } = useAgentList();
  const [status, setStatus] = useState('');
  const [agentId, setAgentId] = useState('');
  const [since, setSince] = useState('');
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [live, setLive] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<number | null>(null);
  const [confirmCancel, setConfirmCancel] = useState<TaskListItem | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const inFlight = useRef(false);
  const reloadRef = useRef<(() => void) | null>(null);
  const toast = useToast();

  // Effect re-runs whenever the filters change; it also installs the 5s long-poll timer.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    const controller = new AbortController();

    async function load(showSpinner: boolean) {
      if (inFlight.current) return;
      inFlight.current = true;
      if (showSpinner) setLoading(true);
      try {
        const resp = await listTasks(
          { status: status || undefined, agent_id: agentId.trim() || undefined, since: since.trim() || undefined, limit: 50 },
          controller.signal,
        );
        setTasks(resp.tasks ?? []);
        setError(null);
        setLastRefresh(Date.now());
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) setError(err);
      } finally {
        inFlight.current = false;
        if (showSpinner) setLoading(false);
      }
    }

    void load(true);
    reloadRef.current = () => void load(false);
    if (live) {
      timer = setInterval(() => void load(false), config.taskFeedPollMs);
    }
    return () => {
      controller.abort();
      if (timer) clearInterval(timer);
      reloadRef.current = null;
    };
  }, [status, agentId, since, live]);

  // Silent, in-place refresh reusing the effect's current loader (no full-table spinner).
  function reload() {
    reloadRef.current?.();
  }

  async function onCancel(task: TaskListItem) {
    setCancelling(true);
    try {
      await cancelTask(task.task_id);
      toast.success('Task cancelled.');
      setConfirmCancel(null);
      reload();
    } catch (err) {
      if (err instanceof BffError && err.status === 409) {
        toast.info('Task already finished.');
        setConfirmCancel(null);
        reload();
      } else {
        toast.error(err instanceof Error ? err.message : 'Could not cancel this task.');
      }
    } finally {
      setCancelling(false);
    }
  }

  const columns: Array<Column<TaskListItem>> = [
    {
      key: 'task_id',
      header: 'Task',
      render: (t) => (
        <span className="inline-flex" onClick={(e) => e.stopPropagation()}>
          <CopyButton value={t.task_id} label="Copy Task ID" />
        </span>
      ),
    },
    { key: 'agent', header: 'Agent', render: (t) => <AgentName agentId={t.agent_id} /> },
    { key: 'status', header: 'Status', render: (t) => <StatusBadge status={t.status} /> },
    {
      key: 'error',
      header: 'Error',
      render: (t) => (t.error_code ? <Badge tone="danger">{t.error_code}</Badge> : <span className="text-muted">—</span>),
    },
    { key: 'tokens', header: 'Tokens', className: 'text-right', render: (t) => <span className="font-mono text-xs tabular-nums">{formatNumber(t.tokens_used)}</span> },
    { key: 'cost', header: 'Cost', className: 'text-right', render: (t) => <span className="font-mono text-xs tabular-nums">{formatCost(t.cost_usd)}</span> },
    { key: 'created', header: 'Created', className: 'text-right', render: (t) => <span className="text-xs text-muted">{formatTime(t.created_at)}</span> },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (t) =>
        t.status === 'pending' || t.status === 'running' ? (
          <Button
            size="sm"
            variant="secondary"
            onClick={(e) => {
              e.stopPropagation();
              setConfirmCancel(t);
            }}
          >
            Cancel
          </Button>
        ) : null,
    },
  ];

  return (
    <Page>
      <PageHeader
        title="Task Feed"
        description={`Live feed with a ${config.taskFeedPollMs / 1000}s long-poll.`}
        actions={
          <div className="flex items-center gap-2.5">
            {lastRefresh && live && (
              <span className="hidden items-center gap-1.5 text-xs text-muted sm:inline-flex">
                <span className={cn('h-1.5 w-1.5 rounded-full', 'bg-success cx-ring')} />
                Updated {formatTime(new Date(lastRefresh).toISOString())}
              </span>
            )}
            <Button size="md" variant={live ? 'secondary' : 'primary'} onClick={() => setLive((v) => !v)}>
              {live ? 'Pause Live' : 'Resume Live'}
            </Button>
          </div>
        }
      />

      <PageBody fill>
      <Card className="mb-3 shrink-0">
        <CardBody>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Select label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABEL[s] ?? s}
                </option>
              ))}
            </Select>
            <Select label="Agent" value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              <option value="">All Agents</option>
              {agents.map((a) => (
                <option key={a.agent_id} value={a.agent_id}>
                  {a.name}
                </option>
              ))}
            </Select>
            <Input label="Since (ISO)" placeholder="2026-06-01T00:00:00Z" value={since} onChange={(e) => setSince(e.target.value)} />
          </div>
        </CardBody>
      </Card>

      {error ? <ErrorBanner error={error} title="Could not load the task feed" className="mb-3 shrink-0" /> : null}

      <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <CardHeader title="Tasks" />
        <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
          {loading ? (
            <Loading label="Loading tasks…" />
          ) : (
            <Table
              columns={columns}
              rows={tasks}
              rowKey={(t) => t.task_id}
              onRowClick={(t) => router.push(`/tasks/${t.task_id}`)}
              empty="No tasks match these filters."
            />
          )}
        </CardBody>
      </Card>
      </PageBody>

      <ConfirmDialog
        open={confirmCancel !== null}
        onClose={() => setConfirmCancel(null)}
        onConfirm={() => confirmCancel && onCancel(confirmCancel)}
        title="Cancel This Task?"
        description="This stops the run where it is."
        confirmLabel="Cancel Task"
        cancelLabel="Keep Running"
        loading={cancelling}
      >
        {confirmCancel && (
          <p className="flex flex-wrap items-center gap-2 text-sm text-muted">
            This task will stop executing and be marked cancelled.
            <CopyButton value={confirmCancel.task_id} label="Copy Task ID" />
          </p>
        )}
      </ConfirmDialog>
    </Page>
  );
}
