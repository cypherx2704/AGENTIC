'use client';

import { useState } from 'react';
import type { ReactNode } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  ErrorBanner,
  Input,
  Loading,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { AccessModePanel } from '@/components/AccessModePanel';
import { useAsync } from '@/lib/useAsync';
import { getSkill, getSkillAccess, listAgents, markSkillRestricted, setSkillAccess } from '@/lib/services';
import type { SkillView } from '@/lib/types';

/** SkillView carries an index signature and permissive fields — coerce untyped values safely. */
function asString(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

function ownerOf(s: SkillView): 'platform' | 'tenant' {
  const o = asString(s.owner);
  if (o === 'platform' || o === 'tenant') return o;
  return s.is_platform ? 'platform' : 'tenant';
}

function versionOf(s: SkillView): string {
  return asString(s.resolved_version) ?? asString(s.version) ?? asString(s.latest_version) ?? '—';
}

/** Capabilities are capability-name strings in the view, but the type admits objects — handle both. */
function capName(c: unknown): string {
  if (typeof c === 'string') return c || '—';
  if (c && typeof c === 'object') {
    const o = c as Record<string, unknown>;
    return asString(o.capability) ?? asString(o.name) ?? asString(o.id) ?? '—';
  }
  return '—';
}
function capDescription(c: unknown): string | undefined {
  if (c && typeof c === 'object') {
    const o = c as Record<string, unknown>;
    return asString(o.description) ?? asString(o.summary);
  }
  return undefined;
}
function capScope(c: unknown): string | undefined {
  if (c && typeof c === 'object') {
    const o = c as Record<string, unknown>;
    return asString(o.required_scope) ?? asString(o.scope);
  }
  return undefined;
}

/** Route params arrive decoded, but decode defensively (never throw on a stray '%'). */
function safeDecode(v: string): string {
  try {
    return decodeURIComponent(v);
  } catch {
    return v;
  }
}

export default function SkillDetailPage() {
  const params = useParams<{ name: string }>();
  const name = safeDecode(params.name);
  const toast = useToast();

  const { data: skill, loading, error } = useAsync<SkillView>((signal) => getSkill(name, undefined, signal), [name]);

  // Agents for the access picker — best-effort; on failure the panel falls back to a free-text id.
  const { data: agents } = useAsync(async (signal) => {
    const res = await listAgents({ limit: 100 }, signal);
    const list = res.items ?? res.agents ?? res.data ?? [];
    return list.map((a) => ({ agent_id: a.agent_id, name: a.name || a.agent_id }));
  }, [name]);

  const [restrictOpen, setRestrictOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [restricting, setRestricting] = useState(false);

  async function onMarkRestricted() {
    setRestricting(true);
    try {
      await markSkillRestricted(name, reason.trim() || undefined);
      toast.success('Skill marked as restricted.');
      setRestrictOpen(false);
      setReason('');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not mark the skill as restricted.');
    } finally {
      setRestricting(false);
    }
  }

  const capabilities: unknown[] = skill && Array.isArray(skill.capabilities) ? skill.capabilities : [];
  const scopes: string[] =
    skill && Array.isArray(skill.required_scopes) ? skill.required_scopes.map((s) => String(s)).filter(Boolean) : [];

  const capColumns: Array<Column<unknown>> = [
    {
      key: 'capability',
      header: 'Capability',
      render: (c) => <span className="font-mono text-xs text-fg">{capName(c)}</span>,
    },
    {
      key: 'description',
      header: 'Description',
      render: (c) => <span className="text-muted">{capDescription(c) ?? '—'}</span>,
    },
    {
      key: 'scope',
      header: 'Required Scope',
      render: (c) => {
        const sc = capScope(c);
        return sc ? <span className="font-mono text-xs text-muted">{sc}</span> : <span className="text-xs text-muted">—</span>;
      },
    },
  ];

  return (
    <Page>
      <PageHeader
        title={name}
        description="Skill registry entry"
        actions={
          <>
            {skill && (
              <>
                <Badge tone={ownerOf(skill) === 'platform' ? 'info' : 'neutral'}>
                  {ownerOf(skill) === 'platform' ? 'Platform' : 'Tenant'}
                </Badge>
                <Badge tone="neutral" className="font-mono">
                  {versionOf(skill)}
                </Badge>
              </>
            )}
            <Link href="/skills" className="text-[13px] font-medium text-brand hover:underline">
              ← Back To Skills
            </Link>
          </>
        }
      />

      <PageBody>
      {error ? (
        <ErrorBanner error={error} title="Could not load this skill" />
      ) : loading ? (
        <Loading label="Loading skill…" />
      ) : skill ? (
        <div className="flex flex-col gap-3">
          <Card>
            <CardHeader title="Overview" />
            <CardBody>
              <dl className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-2">
                <Field label="Owner" value={ownerOf(skill) === 'platform' ? 'Platform' : 'Tenant'} />
                <Field label="Resolved Version" value={<span className="font-mono text-xs">{versionOf(skill)}</span>} />
                <Field
                  label="Latest Version"
                  value={<span className="font-mono text-xs">{asString(skill.latest_version) ?? versionOf(skill)}</span>}
                />
                <Field
                  label="Invoke URL"
                  value={<span className="break-all font-mono text-xs">{asString(skill.invoke_url) ?? '—'}</span>}
                />
                <div className="sm:col-span-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">Required Scopes</p>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {scopes.length ? (
                      scopes.map((sc) => (
                        <Badge key={sc} tone="neutral" className="font-mono">
                          {sc}
                        </Badge>
                      ))
                    ) : (
                      <span className="text-sm text-muted">None</span>
                    )}
                  </div>
                </div>
              </dl>

              <div className="mt-4 rounded-md border border-border bg-surface-2 px-4 py-3">
                <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">Description</p>
                <p className="whitespace-pre-wrap text-sm text-fg">{asString(skill.description) ?? 'No description provided.'}</p>
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Capabilities" description="What agents can invoke on this skill." />
            <CardBody className="px-0 py-0">
              <Table
                columns={capColumns}
                rows={capabilities}
                rowKey={(c, i) => `${capName(c)}-${i}`}
                empty="This skill declares no capabilities."
              />
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Manifest" description="The resolved skill manifest." />
            <CardBody>
              {skill.manifest ? (
                <pre className="overflow-x-auto rounded-md border border-border bg-surface-2 px-4 py-3 font-mono text-xs leading-relaxed text-fg">
                  {JSON.stringify(skill.manifest, null, 2)}
                </pre>
              ) : (
                <p className="text-sm text-muted">No manifest available.</p>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Access" description="Resolve and set per-agent access for this skill." />
            <CardBody className="flex flex-col gap-4">
              <AccessModePanel
                resourceLabel="skill"
                agents={agents ?? []}
                resolve={(id) => getSkillAccess(name, { agent_id: id })}
                apply={(id, mode) => setSkillAccess(name, { agent_id: id, access_mode: mode })}
              />

              <div className="flex flex-col gap-2 border-t border-border pt-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-fg">Restrict This Skill</p>
                    <p className="text-xs text-muted">
                      Restricted skills require explicit per-agent authorization before any agent may run them.
                    </p>
                  </div>
                  <Button variant="secondary" size="md" onClick={() => setRestrictOpen(true)}>
                    Mark As Restricted
                  </Button>
                </div>
              </div>
            </CardBody>
          </Card>
        </div>
      ) : null}
      </PageBody>

      <ConfirmDialog
        open={restrictOpen}
        onClose={() => setRestrictOpen(false)}
        onConfirm={onMarkRestricted}
        title="Mark This Skill As Restricted?"
        description="Agents will need explicit authorization to run it."
        confirmLabel="Mark As Restricted"
        confirmVariant="primary"
        loading={restricting}
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted">
            <span className="font-medium text-fg">{name}</span> will be gated until each agent is individually granted
            access.
          </p>
          <Input
            label="Reason (Optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why is this skill being restricted?"
            autoComplete="off"
            disabled={restricting}
          />
        </div>
      </ConfirmDialog>
    </Page>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</p>
      <p className="mt-1 text-fg">{value}</p>
    </div>
  );
}
