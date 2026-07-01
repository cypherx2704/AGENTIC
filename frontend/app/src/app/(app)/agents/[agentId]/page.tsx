'use client';

import Link from 'next/link';
import { use } from 'react';
import { PageHeader } from '@/components/AppShell';
import { Badge, Card, CardBody, CardHeader, ErrorBanner, Loading, StatusBadge } from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import { useAsync } from '@/lib/useAsync';
import { getAgent, getRuntime, listModels } from '@/lib/services';
import type { AgentRuntime } from '@/lib/types';
import { formatTime } from '@/lib/utils';
import { AgentBuilder } from './AgentBuilder';

export default function AgentDetailPage({ params }: { params: Promise<{ agentId: string }> }) {
  const { agentId } = use(params);

  const agentQ = useAsync((signal) => getAgent(agentId, signal), [agentId]);
  const modelsQ = useAsync((signal) => listModels(signal), []);
  // A 404 here is expected (no runtime registered yet) — treat it as "null runtime".
  const runtimeQ = useAsync<AgentRuntime | null>(
    (signal) =>
      getRuntime(agentId, signal).catch((err) => {
        if (err instanceof BffError && err.status === 404) return null;
        throw err;
      }),
    [agentId],
  );

  const agent = agentQ.data;
  const models = modelsQ.data?.data ?? [];

  return (
    <div>
      <PageHeader
        title={agent ? agent.name : 'Agent'}
        description={<span className="font-mono text-xs">{agentId}</span>}
        actions={
          <Link href="/agents" className="text-sm text-brand hover:underline">
            ← All agents
          </Link>
        }
      />

      {agentQ.error ? (
        <ErrorBanner error={agentQ.error} title="Could not load this agent" className="mb-4" />
      ) : null}

      {agentQ.loading ? (
        <Loading label="Loading agent…" />
      ) : (
        <div className="flex flex-col gap-6">
          {agent && (
            <Card>
              <CardHeader title="Identity" description="From the Auth service (the source of truth for an agent's tenant)." />
              <CardBody>
                <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                  <Field label="Status" value={<StatusBadge status={agent.status} />} />
                  <Field label="Version" value={agent.version || '—'} />
                  <Field label="Created" value={formatTime(agent.created_at)} />
                  <Field label="Updated" value={formatTime(agent.updated_at)} />
                  <div className="col-span-2 sm:col-span-4">
                    <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">Allowed scopes</p>
                    <div className="flex flex-wrap gap-1">
                      {(agent.allowed_scopes ?? []).map((s) => (
                        <Badge key={s} tone="info">
                          {s}
                        </Badge>
                      ))}
                      {(agent.allowed_scopes?.length ?? 0) === 0 && <span className="text-sm text-muted">none</span>}
                    </div>
                  </div>
                </dl>
              </CardBody>
            </Card>
          )}

          {runtimeQ.loading ? (
            <Loading label="Loading runtime config…" />
          ) : runtimeQ.error ? (
            <ErrorBanner error={runtimeQ.error} title="Could not load runtime config" />
          ) : (
            <AgentBuilder
              agentId={agentId}
              fallbackName={agent?.name ?? agentId}
              initialRuntime={runtimeQ.data}
              models={models}
              onSaved={(rt) => runtimeQ.setData(rt)}
            />
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p className="mt-1 text-sm text-fg">{value}</p>
    </div>
  );
}
