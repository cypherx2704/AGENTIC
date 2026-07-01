'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  ErrorBanner,
  Input,
  Loading,
  Select,
  StatusBadge,
  Table,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { listTasks } from '@/lib/services';
import type { TaskListItem } from '@/lib/types';
import { config } from '@/lib/config';
import { formatCost, formatNumber, formatTime, shortId } from '@/lib/utils';

const STATUSES = ['', 'pending', 'running', 'completed', 'failed', 'cancelled', 'timeout'];

export default function TaskFeedPage() {
  const router = useRouter();
  const [status, setStatus] = useState('');
  const [agentId, setAgentId] = useState('');
  const [since, setSince] = useState('');
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [live, setLive] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<number | null>(null);
  const inFlight = useRef(false);

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
    if (live) {
      timer = setInterval(() => void load(false), config.taskFeedPollMs);
    }
    return () => {
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [status, agentId, since, live]);

  const columns: Array<Column<TaskListItem>> = [
    { key: 'task_id', header: 'Task', render: (t) => <span className="font-mono text-xs text-fg">{shortId(t.task_id, 12)}</span> },
    { key: 'agent', header: 'Agent', render: (t) => <span className="font-mono text-xs text-muted">{shortId(t.agent_id, 10)}</span> },
    { key: 'status', header: 'Status', render: (t) => <StatusBadge status={t.status} /> },
    {
      key: 'error',
      header: 'Error',
      render: (t) => (t.error_code ? <Badge tone="danger">{t.error_code}</Badge> : <span className="text-muted">—</span>),
    },
    { key: 'tokens', header: 'Tokens', render: (t) => <span className="font-mono text-xs">{formatNumber(t.tokens_used)}</span> },
    { key: 'cost', header: 'Cost', render: (t) => <span className="font-mono text-xs">{formatCost(t.cost_usd)}</span> },
    { key: 'created', header: 'Created', render: (t) => <span className="text-xs text-muted">{formatTime(t.created_at)}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Task feed"
        description={`Live feed with a ${config.taskFeedPollMs / 1000}s long-poll.`}
        actions={
          <div className="flex items-center gap-2">
            {lastRefresh && live && <span className="text-xs text-muted">updated {formatTime(new Date(lastRefresh).toISOString())}</span>}
            <Button size="sm" variant={live ? 'secondary' : 'primary'} onClick={() => setLive((v) => !v)}>
              {live ? 'Pause' : 'Resume'} live
            </Button>
          </div>
        }
      />

      <Card className="mb-4">
        <CardBody>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Select label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s || 'all'}
                </option>
              ))}
            </Select>
            <Input label="Agent ID" placeholder="filter by agent" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
            <Input label="Since (ISO)" placeholder="2026-06-01T00:00:00Z" value={since} onChange={(e) => setSince(e.target.value)} />
          </div>
        </CardBody>
      </Card>

      {error ? <ErrorBanner error={error} title="Could not load the task feed" className="mb-4" /> : null}

      <Card>
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
      </Card>
    </div>
  );
}
