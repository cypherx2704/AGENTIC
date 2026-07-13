'use client';

import { useState } from 'react';
import type { ReactNode } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AccessModePanel } from '@/components/AccessModePanel';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  ErrorBanner,
  Loading,
  StatusBadge,
  Table,
  Textarea,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { getTool, getToolAccess, listAgents, markToolRestricted, setToolAccess } from '@/lib/services';
import type { ToolView } from '@/lib/types';
import { useAsync } from '@/lib/useAsync';

// ── defensive field readers (ToolView is permissive + may be partial) ─────────────────
function asStr(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

function resolveVersion(t: ToolView): string {
  return asStr(t.resolved_version) ?? asStr(t.version) ?? asStr(t.latest_version) ?? '—';
}

function healthStatus(health: unknown): string | undefined {
  if (typeof health === 'string') return health;
  if (health && typeof health === 'object') {
    const s = (health as Record<string, unknown>).status;
    if (typeof s === 'string') return s;
  }
  return undefined;
}

function ownerKind(t: ToolView): 'platform' | 'tenant' | null {
  if (typeof t.is_platform === 'boolean') return t.is_platform ? 'platform' : 'tenant';
  if (t.owner === 'platform' || t.owner === 'tenant') return t.owner;
  return null;
}

function OwnerBadge({ tool }: { tool: ToolView }) {
  const kind = ownerKind(tool);
  if (kind === 'platform') return <Badge tone="info">Platform</Badge>;
  if (kind === 'tenant') return <Badge>Tenant</Badge>;
  return <span className="text-faint">—</span>;
}

/** Params may arrive percent-encoded; decode defensively (never throw on a stray %). */
function safeDecode(v: string): string {
  try {
    return decodeURIComponent(v);
  } catch {
    return v;
  }
}

// ── capabilities: the view returns capability NAMES (strings) but the type admits objects;
//    normalize both, enriching a bare name from the manifest's tools[] when possible. ─────
interface CapRow {
  name: string;
  required_scope?: string;
  description?: string;
}

function manifestToolsByName(manifest: Record<string, unknown> | null): Map<string, Record<string, unknown>> {
  const map = new Map<string, Record<string, unknown>>();
  const tools = manifest && Array.isArray(manifest.tools) ? manifest.tools : [];
  for (const mt of tools) {
    if (mt && typeof mt === 'object') {
      const n = (mt as Record<string, unknown>).name;
      if (typeof n === 'string') map.set(n, mt as Record<string, unknown>);
    }
  }
  return map;
}

function normalizeCapabilities(tool: ToolView, manifest: Record<string, unknown> | null): CapRow[] {
  const caps: unknown[] = Array.isArray(tool.capabilities) ? tool.capabilities : [];
  const byName = manifestToolsByName(manifest);
  return caps.map((cap): CapRow => {
    if (typeof cap === 'string') {
      const mt = byName.get(cap);
      return { name: cap, description: asStr(mt?.description), required_scope: asStr(mt?.required_scope) };
    }
    if (cap && typeof cap === 'object') {
      const c = cap as Record<string, unknown>;
      const name = asStr(c.name) ?? asStr(c.capability) ?? '—';
      const mt = byName.get(name);
      return {
        name,
        required_scope: asStr(c.required_scope) ?? asStr(mt?.required_scope),
        description: asStr(c.description) ?? asStr(mt?.description),
      };
    }
    return { name: '—' };
  });
}

interface ToolDetail {
  tool: ToolView;
  agents: Array<{ agent_id: string; name: string }>;
}

export default function ToolDetailPage() {
  const { name: rawName } = useParams<{ name: string }>();
  const name = safeDecode(rawName ?? '');
  const toast = useToast();

  const { data, loading, error, reload } = useAsync<ToolDetail>(async (signal) => {
    // The tool is authoritative; the agent list only powers the access picker, so a failed
    // agent fetch degrades to a free-text agent-id input rather than blanking the page.
    const [toolR, agentsR] = await Promise.allSettled([
      getTool(name, undefined, signal),
      listAgents({ limit: 100 }, signal),
    ]);
    if (toolR.status === 'rejected') throw toolR.reason;
    const rawAgents =
      agentsR.status === 'fulfilled'
        ? (agentsR.value.items ?? agentsR.value.agents ?? agentsR.value.data ?? [])
        : [];
    const agents = rawAgents.map((a) => ({ agent_id: a.agent_id, name: a.name ?? a.agent_id }));
    return { tool: toolR.value, agents };
  }, [name]);

  const [confirmRestrict, setConfirmRestrict] = useState(false);
  const [restrictReason, setRestrictReason] = useState('');
  const [restricting, setRestricting] = useState(false);

  const tool = data?.tool ?? null;
  const toolId = asStr(tool?.tool_id);
  const manifest = tool && tool.manifest && typeof tool.manifest === 'object' ? (tool.manifest as Record<string, unknown>) : null;
  const description = asStr(tool?.description) ?? asStr(manifest?.description);
  const rawScopes = tool?.required_scopes;
  const scopes = Array.isArray(rawScopes) ? rawScopes.filter((s): s is string => typeof s === 'string') : [];
  const caps = tool ? normalizeCapabilities(tool, manifest) : [];
  const versionLabel = tool ? resolveVersion(tool) : '—';

  async function onRestrict() {
    setRestricting(true);
    try {
      await markToolRestricted(name, restrictReason.trim() || undefined);
      toast.success(`Marked ${name} as restricted.`);
      setConfirmRestrict(false);
      setRestrictReason('');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not mark this tool as restricted.');
    } finally {
      setRestricting(false);
    }
  }

  const capColumns: Array<Column<CapRow>> = [
    { key: 'name', header: 'Capability', render: (c) => <span className="font-mono text-xs text-fg">{c.name}</span> },
    {
      key: 'scope',
      header: 'Required Scope',
      render: (c) => <span className="font-mono text-xs text-muted">{c.required_scope ?? '—'}</span>,
    },
    { key: 'desc', header: 'Description', render: (c) => <span className="text-muted">{c.description ?? '—'}</span> },
  ];

  return (
    <Page>
      <PageHeader
        title={tool?.name ?? name}
        description={toolId ? <CopyButton value={toolId} label="Copy Tool ID" /> : undefined}
        actions={
          <>
            {tool ? <OwnerBadge tool={tool} /> : null}
            {tool && versionLabel !== '—' ? <Badge>{versionLabel}</Badge> : null}
            <Link href="/tools" className="text-[13px] font-medium text-brand hover:underline">
              ← Back To Tools
            </Link>
          </>
        }
      />

      <PageBody>
      {error ? (
        <ErrorBanner error={error} title="Could not load this tool" />
      ) : loading ? (
        <Loading label="Loading tool…" />
      ) : data && tool ? (
        <div className="flex flex-col gap-3">
          {/* Overview */}
          <Card>
            <CardHeader title="Overview" />
            <CardBody>
              {description ? <p className="mb-4 text-sm text-fg">{description}</p> : null}
              <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
                <Field label="Owner">
                  <OwnerBadge tool={tool} />
                </Field>
                <Field label="Resolved Version">
                  <span className="font-mono text-xs">{versionLabel}</span>
                </Field>
                <Field label="Latest Version">
                  <span className="font-mono text-xs">{asStr(tool.latest_version) ?? '—'}</span>
                </Field>
                <Field label="Health">
                  <StatusBadge status={healthStatus(tool.health)} />
                </Field>
                <Field label="Status">
                  <StatusBadge status={asStr(tool.status)} />
                </Field>
              </dl>

              <div className="mt-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">Invoke URL</p>
                <p className="mt-1 break-all font-mono text-xs text-fg">{asStr(tool.invoke_url) ?? '—'}</p>
              </div>

              <div className="mt-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">Required Scopes</p>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {scopes.length ? (
                    scopes.map((s) => (
                      <Badge key={s} className="font-mono">
                        {s}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-sm text-muted">—</span>
                  )}
                </div>
              </div>
            </CardBody>
          </Card>

          {/* Capabilities */}
          <Card>
            <CardHeader title="Capabilities" description="Invocable capabilities this tool server exposes." />
            <CardBody className="px-0 py-0">
              <Table
                columns={capColumns}
                rows={caps}
                rowKey={(c, i) => `${c.name}-${i}`}
                empty="This tool declares no capabilities."
              />
            </CardBody>
          </Card>

          {/* Manifest */}
          <Card>
            <CardHeader title="Manifest" description="The raw Contract-4 MCP manifest for the resolved version." />
            <CardBody>
              {manifest ? (
                <div className="overflow-x-auto rounded-md border border-border bg-surface-2">
                  <pre className="whitespace-pre-wrap px-3 py-2.5 font-mono text-xs text-fg">
                    {JSON.stringify(manifest, null, 2)}
                  </pre>
                </div>
              ) : (
                <p className="text-sm text-muted">No manifest available for this tool.</p>
              )}
            </CardBody>
          </Card>

          {/* Access control */}
          <Card>
            <CardHeader title="Access Control" description="Set which agents can discover and invoke this tool, and how." />
            <CardBody>
              <AccessModePanel
                resourceLabel="tool"
                agents={data.agents}
                resolve={(agentId) => getToolAccess(name, { agent_id: agentId })}
                apply={(agentId, mode) => setToolAccess(name, { agent_id: agentId, access_mode: mode })}
              />

              <div className="mt-4 flex items-start justify-between gap-3 border-t border-border pt-4">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-fg">Restricted Access</p>
                  <p className="mt-0.5 text-xs text-muted">
                    Require an explicit per-agent grant before any agent can invoke this tool.
                  </p>
                </div>
                <Button variant="danger" size="md" onClick={() => setConfirmRestrict(true)}>
                  Mark As Restricted
                </Button>
              </div>

              {tool.restricted ? (
                <Callout tone="warning" className="mt-3">
                  This tool is currently restricted — agents need an explicit access grant above.
                </Callout>
              ) : null}
            </CardBody>
          </Card>
        </div>
      ) : null}
      </PageBody>

      <ConfirmDialog
        open={confirmRestrict}
        onClose={() => setConfirmRestrict(false)}
        onConfirm={onRestrict}
        title="Mark This Tool As Restricted?"
        description="Restricted tools require an explicit per-agent access grant before any agent can invoke them."
        confirmLabel="Mark As Restricted"
        loading={restricting}
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted">
            <span className="font-medium text-fg">{name}</span> will be hidden from automated discovery access until you
            grant specific agents access on this page.
          </p>
          <Textarea
            label="Reason (Optional)"
            value={restrictReason}
            onChange={(e) => setRestrictReason(e.target.value)}
            placeholder="Why is this tool being restricted?"
            className="min-h-[60px] font-sans"
            disabled={restricting}
          />
        </div>
      </ConfirmDialog>
    </Page>
  );
}

/** Small dt/dd field used in the overview grid. */
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</p>
      <div className="mt-1 text-sm text-fg">{children}</div>
    </div>
  );
}
