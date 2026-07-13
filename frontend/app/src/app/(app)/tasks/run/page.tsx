'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { Pipeline } from '@/components/Pipeline';
import { TaskTimeline } from '@/components/TaskTimeline';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyButton,
  ErrorBanner,
  Input,
  Select,
  Stat,
  StatusBadge,
  Switch,
  Textarea,
  useToast,
} from '@/components/ui';
import { BffError, streamUrl } from '@/lib/bff-client';
import { cancelTask, getTask, submitTask } from '@/lib/services';
import { useAgentList } from '@/lib/useAgentList';
import { CANONICAL_STAGES, stepsToStages } from '@/lib/pipeline';
import type { TaskResponse, TaskStep } from '@/lib/types';
import { formatCost, formatNumber } from '@/lib/utils';

interface BlockedInfo {
  message: string;
  code: string;
  traceId?: string;
}

export default function TaskRunnerPage() {
  const [agentId, setAgentId] = useState('');
  const [message, setMessage] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [timeoutSec, setTimeoutSec] = useState('');
  const [testRun, setTestRun] = useState(true);
  const [useStream, setUseStream] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [blocked, setBlocked] = useState<BlockedInfo | null>(null);
  const [task, setTask] = useState<TaskResponse | null>(null);
  const [liveSteps, setLiveSteps] = useState<TaskStep[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const toast = useToast();
  const { agents, loading: agentsLoading } = useAgentList();

  const closeStream = useCallback(() => {
    esRef.current?.close();
    esRef.current = null;
    setStreaming(false);
  }, []);

  useEffect(() => () => closeStream(), [closeStream]);

  /**
   * Open the SSE stream for a running task and update the live step timeline. SSE rides
   * the same httpOnly session cookie (withCredentials) — no token in the browser.
   */
  function openStream(taskId: string) {
    closeStream();
    setStreaming(true);
    const url = streamUrl('xagent', `/v1/tasks/${taskId}/stream`);
    const es = new EventSource(url, { withCredentials: true });
    esRef.current = es;

    const applySnapshot = (data: any) => {
      if (Array.isArray(data?.task_steps)) {
        setLiveSteps(
          data.task_steps.map((s: any) => ({
            step: s.step,
            status: s.status,
            duration_ms: s.duration_ms ?? null,
            tokens: s.tokens ?? null,
          })),
        );
      }
    };

    es.addEventListener('snapshot', (e) => applySnapshot(safeParse((e as MessageEvent).data)));
    es.addEventListener('step', (e) => {
      const d = safeParse((e as MessageEvent).data);
      if (d?.step) {
        setLiveSteps((prev) => [...prev, { step: d.step, status: d.status, duration_ms: d.duration_ms, tokens: d.tokens }]);
      }
    });
    const finalize = async (e: Event) => {
      const d = safeParse((e as MessageEvent).data);
      closeStream();
      // The terminal frame carries the full result; otherwise re-fetch the canonical row.
      if (d?.result) {
        setTask(d.result as TaskResponse);
        if (d.result.error?.code === 'GUARDRAIL_VIOLATION') {
          setBlocked({ message: d.result.error.message, code: d.result.error.code, traceId: d.result.error.trace_id });
        }
      } else {
        try {
          setTask(await getTask(taskId));
        } catch {
          /* keep live steps */
        }
      }
    };
    es.addEventListener('done', finalize);
    es.addEventListener('error', (e) => {
      // EventSource fires a generic 'error' on network close too; only treat it as terminal
      // if it carries a data payload (the server's terminal error frame).
      if ((e as MessageEvent).data) void finalize(e);
    });
    es.addEventListener('content_filter', (e) => {
      const d = safeParse((e as MessageEvent).data);
      setBlocked({
        message: d?.error?.message ?? 'Guardrail blocked this task.',
        code: d?.error?.code ?? 'GUARDRAIL_VIOLATION',
        traceId: d?.error?.trace_id,
      });
      void finalize(e);
    });
  }

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setBlocked(null);
    setTask(null);
    setLiveSteps([]);
    closeStream();

    try {
      const resp = await submitTask({
        agent_id: agentId.trim(),
        input: { message },
        // metadata.test marks this as an operator test run (reserved metadata key).
        metadata: { test: testRun },
        session_id: sessionId.trim() || undefined,
        timeout_seconds: timeoutSec.trim() ? Number(timeoutSec) : undefined,
      });
      setTask(resp);
      // If async/streaming and still running, attach the SSE stream for the live timeline.
      if (useStream && (resp.status === 'running' || resp.status === 'pending')) {
        openStream(resp.task_id);
      }
    } catch (err) {
      if (err instanceof BffError && err.isGuardrailViolation) {
        // 422 guardrail block — show the dedicated blocked banner + the (failed) timeline.
        const taskId = (err.details?.task_id as string | undefined) ?? undefined;
        setBlocked({ message: err.message, code: err.code, traceId: err.traceId });
        if (taskId) {
          try {
            setTask(await getTask(taskId));
          } catch {
            setLiveSteps([{ step: 'guardrail_check_input', status: 'failed', duration_ms: null, tokens: null }]);
          }
        } else {
          setLiveSteps([{ step: 'guardrail_check_input', status: 'failed', duration_ms: null, tokens: null }]);
        }
      } else {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  }

  const steps = task?.task_steps?.length ? task.task_steps : liveSteps;
  const stages = stepsToStages(steps);
  const canCancel = !!task && (task.status === 'running' || task.status === 'pending');

  // Fire-and-observe: DELETE the task, then let the live SSE/finalize path surface the cancelled state.
  async function onCancel() {
    if (!task) return;
    setCancelling(true);
    try {
      await cancelTask(task.task_id);
      toast.success('Task cancelled.');
    } catch (err) {
      if (err instanceof BffError && err.status === 409) {
        toast.info('Task already finished.');
      } else {
        toast.error(err instanceof Error ? err.message : 'Could not cancel this task.');
      }
    } finally {
      setCancelling(false);
    }
  }

  return (
    <Page>
      <PageHeader title="Task Runner" description="Submit a task and watch the live execution pipeline with real cost + tokens." />

      <PageBody>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <Card>
          <CardHeader title="Submit a Task" />
          <CardBody>
            <form onSubmit={run} className="flex flex-col gap-4">
              <Select
                label="Agent"
                value={agentId}
                onChange={(e) => setAgentId(e.target.value)}
                required
                disabled={agentsLoading || agents.length === 0}
                hint={
                  agentsLoading
                    ? 'Loading agents…'
                    : agents.length === 0
                      ? 'No active agents available for this tenant.'
                      : 'The agent to run this task against.'
                }
              >
                <option value="">Select an agent…</option>
                {agents.map((a) => (
                  <option key={a.agent_id} value={a.agent_id}>
                    {a.name}
                  </option>
                ))}
              </Select>
              <Textarea
                label="Message"
                placeholder="Ask the agent something… (try a prompt-injection to see the 422 block)"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                required
              />

              <div className="grid grid-cols-2 gap-3">
                <Input
                  label="Session ID"
                  placeholder="optional"
                  value={sessionId}
                  onChange={(e) => setSessionId(e.target.value)}
                  hint="Ties the task to a memory session."
                />
                <Input
                  label="Timeout (s)"
                  type="number"
                  min={1}
                  placeholder="default"
                  value={timeoutSec}
                  onChange={(e) => setTimeoutSec(e.target.value)}
                  hint="Optional per-task deadline."
                />
              </div>

              <div className="flex flex-col gap-3 rounded-md border border-border bg-surface-2 px-3.5 py-3">
                <Switch checked={useStream} onChange={setUseStream} label="Stream Live (SSE)" hint="Watch each stage arrive in real time." />
                <Switch checked={testRun} onChange={setTestRun} label="Mark as Test Run" hint="Tags the task metadata.test = true." />
              </div>

              <div className="flex items-center gap-2">
                <Button type="submit" size="md" loading={busy} disabled={!agentId.trim() || !message.trim()}>
                  Run Task
                </Button>
                {testRun && <Badge>metadata.test = true</Badge>}
              </div>
              {error ? <ErrorBanner error={error} /> : null}
            </form>
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Result"
            description={task ? <CopyButton value={task.task_id} label="Copy Task ID" /> : undefined}
            actions={
              <div className="flex items-center gap-2">
                {canCancel && (
                  <Button variant="secondary" size="sm" loading={cancelling} onClick={() => void onCancel()}>
                    Cancel Task
                  </Button>
                )}
                {streaming ? <Badge tone="warning">Streaming…</Badge> : task ? <StatusBadge status={task.status} /> : null}
              </div>
            }
          />
          <CardBody>
            {task || streaming ? (
              <div className="mb-4 rounded-md border border-border bg-surface px-2 py-3">
                <Pipeline stages={stages.length ? stages : CANONICAL_STAGES} className="px-1" />
              </div>
            ) : null}

            {blocked ? (
              <div className="mb-4 rounded-md border border-warning/50 bg-warning/10 px-4 py-3" role="alert">
                <div className="flex items-center gap-2">
                  <Badge tone="warning">{blocked.code}</Badge>
                  <span className="text-sm font-semibold text-fg">Blocked by a Guardrail (HTTP 422)</span>
                </div>
                <p className="mt-1 text-sm text-fg/90">{blocked.message}</p>
                {blocked.traceId && <p className="mt-1 font-mono text-xs text-muted">trace: {blocked.traceId}</p>}
              </div>
            ) : null}

            {task && (
              <div className="mb-4 grid grid-cols-3 gap-3">
                <Stat label="Tokens" value={formatNumber(task.tokens_used)} />
                <Stat label="Cost" value={formatCost(task.cost_usd)} />
                <Stat label="Steps" value={steps.length} />
              </div>
            )}

            {task?.output?.message ? (
              <div className="mb-4 rounded-md border border-border bg-surface-2 px-4 py-3">
                <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Answer</p>
                <p className="whitespace-pre-wrap text-sm text-fg">{task.output.message}</p>
              </div>
            ) : null}

            {steps.length > 0 || streaming ? (
              <div>
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-faint">Timeline</p>
                <TaskTimeline steps={steps} />
              </div>
            ) : !task && !blocked ? (
              <p className="py-8 text-center text-sm text-muted">Submit a task to see its pipeline here.</p>
            ) : null}

            {task && (
              <div className="mt-4 text-right">
                <Link href={`/tasks/${task.task_id}`} className="text-[13px] font-medium text-brand hover:underline">
                  Open Full Task Detail
                </Link>
              </div>
            )}
          </CardBody>
        </Card>
      </div>
      </PageBody>
    </Page>
  );
}

function safeParse(raw: string): any {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
