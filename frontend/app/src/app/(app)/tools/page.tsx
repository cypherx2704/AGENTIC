'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ErrorBanner,
  Loading,
  Modal,
  StatusBadge,
  Table,
  Textarea,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { listRestrictedTools, listTools, registerTool } from '@/lib/services';
import type { ToolView } from '@/lib/types';
import { useAsync } from '@/lib/useAsync';

// ── defensive field readers (ToolView is permissive + may be partial) ─────────────────
/** Coerce an unknown to a non-empty trimmed string, else undefined. */
function asStr(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

/** Resolved version, tolerating the several fields a gateway may use. */
function resolveVersion(t: ToolView): string {
  return asStr(t.resolved_version) ?? asStr(t.version) ?? asStr(t.latest_version) ?? '—';
}

/** Health may be a bare status string OR an object carrying a `status` field. */
function healthStatus(health: unknown): string | undefined {
  if (typeof health === 'string') return health;
  if (health && typeof health === 'object') {
    const s = (health as Record<string, unknown>).status;
    if (typeof s === 'string') return s;
  }
  return undefined;
}

/** Owner from `is_platform` (bool) first, else the `owner` string; unknown → null. */
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

// ── example manifest shown as the register placeholder ────────────────────────────────
const MANIFEST_PLACEHOLDER = `{
  "schema_version": "1.0.0",
  "protocol_version": "mcp/1.0",
  "name": "tool-my-service",
  "version": "1.0.0",
  "description": "What this tool server does.",
  "base_url": "http://tool-my-service:8080",
  "required_scopes": ["tool:invoke"],
  "tools": [
    {
      "name": "do_thing",
      "description": "Perform the thing.",
      "input_schema": { "type": "object", "properties": {} }
    }
  ]
}`;

interface CatalogData {
  tools: ToolView[];
  restricted: Array<Record<string, unknown>>;
  restrictedError: boolean;
}

/**
 * Load the catalog + the restricted-tools list together. The tools call is authoritative
 * (its failure surfaces on the main table); a failing restricted call only degrades that
 * secondary card, so it is settled independently.
 */
async function loadCatalog(signal: AbortSignal): Promise<CatalogData> {
  const [toolsR, restrictedR] = await Promise.allSettled([listTools({}, signal), listRestrictedTools(signal)]);
  if (toolsR.status === 'rejected') throw toolsR.reason;
  return {
    tools: toolsR.value,
    restricted: restrictedR.status === 'fulfilled' ? (restrictedR.value.data ?? []) : [],
    restrictedError: restrictedR.status === 'rejected',
  };
}

export default function ToolsPage() {
  const { data, loading, error, reload } = useAsync(loadCatalog, []);
  const [registerOpen, setRegisterOpen] = useState(false);

  const tools = data?.tools ?? [];
  const restricted = data?.restricted ?? [];

  const columns: Array<Column<ToolView>> = [
    {
      key: 'name',
      header: 'Name',
      render: (t) => (
        <Link
          href={`/tools/${encodeURIComponent(t.name)}`}
          className="font-medium text-fg hover:text-brand hover:underline"
        >
          {t.name}
        </Link>
      ),
    },
    { key: 'owner', header: 'Owner', render: (t) => <OwnerBadge tool={t} /> },
    {
      key: 'version',
      header: 'Version',
      render: (t) => <span className="font-mono text-xs text-muted">{resolveVersion(t)}</span>,
    },
    {
      key: 'capabilities',
      header: 'Capabilities',
      className: 'text-right',
      render: (t) => (
        <span className="font-mono text-xs tabular-nums text-muted">
          {Array.isArray(t.capabilities) ? t.capabilities.length : 0}
        </span>
      ),
    },
    { key: 'health', header: 'Health', render: (t) => <StatusBadge status={healthStatus(t.health)} /> },
    {
      key: 'restricted',
      header: 'Restricted',
      render: (t) => (t.restricted ? <Badge tone="warning">Restricted</Badge> : <span className="text-faint">—</span>),
    },
  ];

  return (
    <Page>
      <PageHeader title="Tools" description="MCP tool servers your agents can discover and invoke." />

      <PageBody>
        <Card>
          <CardHeader
            title="Tool Registry"
            actions={
              <>
                <Button variant="secondary" size="md" onClick={reload}>
                  Refresh
                </Button>
                <Button size="md" onClick={() => setRegisterOpen(true)}>
                  Register Tool
                </Button>
              </>
            }
          />
          <CardBody className="px-0 py-0">
            {error ? (
              <div className="p-4">
                <ErrorBanner error={error} title="Could not load tools" />
              </div>
            ) : loading ? (
              <Loading label="Loading tools…" />
            ) : (
              <Table
                columns={columns}
                rows={tools}
                rowKey={(t) => asStr(t.tool_id) ?? t.name}
                empty="No tools yet. Register one from a Contract-4 manifest to get started."
              />
            )}
          </CardBody>
        </Card>

        <Card className="mt-3">
          <CardHeader title="Restricted Tools" description="Tools that require an explicit per-agent access grant before use." />
          <CardBody>
            {loading && !data ? (
              <p className="text-sm text-muted">Loading…</p>
            ) : data?.restrictedError ? (
              <p className="text-sm text-muted">Restricted tools are unavailable right now.</p>
            ) : restricted.length === 0 ? (
              <p className="text-sm text-muted">No tools are restricted for this tenant.</p>
            ) : (
              <ul className="flex flex-col">
                {restricted.map((r, i) => {
                  const nm = asStr(r.name) ?? asStr(r.tool_name) ?? asStr(r.tool) ?? '—';
                  const reason = asStr(r.reason);
                  return (
                    <li
                      key={`${nm}-${i}`}
                      className="flex items-start justify-between gap-3 border-t border-border py-2 first:border-t-0"
                    >
                      <div className="min-w-0">
                        {nm === '—' ? (
                          <span className="font-mono text-xs font-medium text-fg">—</span>
                        ) : (
                          <Link
                            href={`/tools/${encodeURIComponent(nm)}`}
                            className="font-mono text-xs font-medium text-fg hover:text-brand hover:underline"
                          >
                            {nm}
                          </Link>
                        )}
                        {reason ? <p className="mt-0.5 text-xs text-muted">{reason}</p> : null}
                      </div>
                      <Badge tone="warning">Restricted</Badge>
                    </li>
                  );
                })}
              </ul>
            )}
          </CardBody>
        </Card>
      </PageBody>

      <RegisterToolModal
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        onRegistered={() => {
          setRegisterOpen(false);
          reload();
        }}
      />
    </Page>
  );
}

/** Modal that parses + validates a Contract-4 JSON manifest, then registers the tool. */
function RegisterToolModal({
  open,
  onClose,
  onRegistered,
}: {
  open: boolean;
  onClose: () => void;
  onRegistered: () => void;
}) {
  const toast = useToast();
  const [text, setText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  function close() {
    if (submitting) return;
    setFormError(null);
    onClose();
  }

  async function submit() {
    setFormError(null);
    let parsed: Record<string, unknown>;
    try {
      const value: unknown = JSON.parse(text);
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error('Manifest must be a JSON object.');
      }
      parsed = value as Record<string, unknown>;
    } catch (err) {
      const msg = err instanceof SyntaxError ? `Invalid JSON: ${err.message}` : err instanceof Error ? err.message : 'Invalid JSON.';
      setFormError(new Error(msg));
      return;
    }
    setSubmitting(true);
    try {
      const res = await registerTool(parsed);
      toast.success(`Registered ${asStr(res.name) ?? asStr(parsed.name) ?? 'tool'}.`);
      setText('');
      setFormError(null);
      onRegistered();
    } catch (err) {
      setFormError(err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Register Tool"
      description="Paste a Contract-4 MCP manifest to register a new tenant tool."
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} loading={submitting} disabled={submitting || !text.trim()}>
            Register Tool
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {formError ? <ErrorBanner error={formError} title="Could not register the tool" /> : null}
        <Textarea
          label="Manifest (JSON)"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={14}
          spellCheck={false}
          placeholder={MANIFEST_PLACEHOLDER}
          hint="Requires tool:admin. Must include schema_version, protocol_version, name (dash-case), version (semver), description, and a non-empty tools[] array."
        />
      </div>
    </Modal>
  );
}
