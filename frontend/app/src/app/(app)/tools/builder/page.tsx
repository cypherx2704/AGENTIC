'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  Select,
  Textarea,
  useToast,
} from '@/components/ui';
import {
  listFlowTools,
  listNoderedFlows,
  openEditorSession,
  publishFlowTool,
  testFlowTool,
  unpublishFlowTool,
} from '@/lib/services';
import type {
  AccessMode,
  FlowTool,
  FlowToolParam,
  FlowToolParamType,
  NoderedFlow,
  PublishFlowToolRequest,
} from '@/lib/types';
import { useAsync } from '@/lib/useAsync';

const ACCESS_HINT: Record<AccessMode, string> = {
  ask: 'Agents must get human approval on each call (recommended).',
  none: 'No agent can call it until a tenant admin grants access.',
  automated: 'Any agent can call it immediately — highest risk.',
};

const SCALAR_TYPES: FlowToolParamType[] = ['string', 'integer', 'number', 'boolean'];

function clampType(t: unknown): FlowToolParamType {
  return SCALAR_TYPES.includes(t as FlowToolParamType) ? (t as FlowToolParamType) : 'string';
}

/** Reconstruct the editable param rows from a stored input_schema (for Edit). */
function paramsFromSchema(schema: Record<string, unknown> | undefined): FlowToolParam[] {
  const props = (schema?.properties ?? {}) as Record<string, { type?: string; description?: string }>;
  const required = new Set((schema?.required as string[] | undefined) ?? []);
  return Object.entries(props).map(([name, spec]) => ({
    name,
    type: clampType(spec?.type),
    required: required.has(name),
    description: spec?.description ?? '',
  }));
}

/** The console's effective theme — an explicit toggle wins, else the OS preference. */
function resolveConsoleTheme(): 'light' | 'dark' {
  if (typeof document === 'undefined') return 'dark';
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'light' || attr === 'dark') return attr;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function ToolBuilderPage() {
  const session = useAsync(() => openEditorSession(), []);
  const tools = useAsync((signal) => listFlowTools(signal), []);
  const [publishOpen, setPublishOpen] = useState(false);
  const [editing, setEditing] = useState<FlowTool | null>(null);
  const [testing, setTesting] = useState<FlowTool | null>(null);
  const [iframeKey, setIframeKey] = useState(0);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const ready = session.data?.ready ?? false;

  /**
   * Keep the embedded editor's theme in lock-step with the console's light/dark toggle. The editor
   * is a same-origin iframe (proxied through the BFF), so we can stamp `data-cx-theme` on its <html>
   * and enable/disable the pre-built `github-dark` stylesheet — cypherx-theme.css tints each mode.
   */
  const syncEditorTheme = useCallback(() => {
    const doc = iframeRef.current?.contentDocument;
    const root = doc?.documentElement;
    if (!root) return; // not loaded yet, or (defensively) cross-origin
    try {
      const theme = resolveConsoleTheme();
      root.setAttribute('data-cx-theme', theme);
      doc.querySelectorAll<HTMLLinkElement>('link[href*="github-dark"]').forEach((link) => {
        link.disabled = theme !== 'dark';
      });
    } catch {
      /* editor still booting — the onLoad handler and observer will retry */
    }
  }, []);

  // Re-sync whenever the console theme changes (toggle → data-theme, or OS preference).
  useEffect(() => {
    syncEditorTheme();
    const observer = new MutationObserver(syncEditorTheme);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    media.addEventListener('change', syncEditorTheme);
    return () => {
      observer.disconnect();
      media.removeEventListener('change', syncEditorTheme);
    };
  }, [syncEditorTheme, iframeKey, ready]);

  function openPublish() {
    setEditing(null);
    setPublishOpen(true);
  }
  function openEdit(tool: FlowTool) {
    setEditing(tool);
    setPublishOpen(true);
  }

  return (
    <Page>
      <PageHeader
        title="Tool Builder"
        description="Build a workflow visually, then publish it as an MCP tool your agents can discover."
        actions={
          <>
            <Button variant="secondary" size="md" onClick={() => setIframeKey((k) => k + 1)}>
              Reload editor
            </Button>
            <Button size="md" onClick={openPublish} disabled={!ready}>
              Publish Tool
            </Button>
          </>
        }
      />

      <PageBody fill>
        <div className="flex h-full min-h-0 gap-3">
          {/* ── Embedded Node-RED editor ─────────────────────────────────── */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-surface">
            {session.error ? (
              <div className="p-4">
                <ErrorBanner error={session.error} title="Could not open the editor" />
              </div>
            ) : session.loading || !ready ? (
              <Loading label="Preparing your workspace…" />
            ) : (
              <iframe
                key={iframeKey}
                ref={iframeRef}
                src="/bff/nodered/"
                title="CypherX Tool Builder editor"
                className="h-full w-full border-0"
                onLoad={syncEditorTheme}
              />
            )}
          </div>

          {/* ── Published-tools rail ─────────────────────────────────────── */}
          <aside className="flex w-80 shrink-0 flex-col gap-3 overflow-y-auto">
            <Callout tone="info" title="How it works">
              Drag an <code className="font-mono text-xs">http in</code> node as the trigger and an{' '}
              <code className="font-mono text-xs">http response</code> node as the output, wire your
              logic between them, click <strong>Deploy</strong>, then <strong>Publish Tool</strong>.
            </Callout>

            <Card>
              <CardHeader
                title="Published tools"
                actions={
                  <Button variant="ghost" size="sm" onClick={tools.reload}>
                    Refresh
                  </Button>
                }
              />
              <CardBody className="px-0 py-0">
                {tools.error ? (
                  <div className="p-4">
                    <ErrorBanner error={tools.error} title="Could not load tools" />
                  </div>
                ) : tools.loading ? (
                  <Loading label="Loading…" />
                ) : (tools.data ?? []).length === 0 ? (
                  <p className="p-4 text-sm text-muted">
                    No tools published yet. Build a workflow and publish it.
                  </p>
                ) : (
                  <ul className="flex flex-col">
                    {(tools.data ?? []).map((t) => (
                      <PublishedToolRow
                        key={t.slug}
                        tool={t}
                        onChanged={tools.reload}
                        onEdit={() => openEdit(t)}
                        onTest={() => setTesting(t)}
                      />
                    ))}
                  </ul>
                )}
              </CardBody>
            </Card>
          </aside>
        </div>
      </PageBody>

      <PublishToolModal
        open={publishOpen}
        editing={editing}
        onClose={() => setPublishOpen(false)}
        onPublished={() => {
          setPublishOpen(false);
          tools.reload();
        }}
      />
      <TestToolModal tool={testing} onClose={() => setTesting(null)} />
    </Page>
  );
}

function PublishedToolRow({
  tool,
  onChanged,
  onEdit,
  onTest,
}: {
  tool: FlowTool;
  onChanged: () => void;
  onEdit: () => void;
  onTest: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function unpublish() {
    setBusy(true);
    try {
      await unpublishFlowTool(tool.slug);
      toast.success(`Unpublished ${tool.display_name}.`);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not unpublish.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-col gap-1.5 border-t border-border p-3 first:border-t-0">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Link
            href={`/tools/${encodeURIComponent(tool.server_name)}`}
            className="block truncate text-sm font-medium text-fg hover:text-brand hover:underline"
          >
            {tool.display_name}
          </Link>
          <p className="mt-0.5 truncate text-xs text-muted">
            <span className="font-mono">{tool.tool_name}</span> · v{tool.version}
          </p>
        </div>
        <Badge tone={tool.access_mode === 'automated' ? 'warning' : 'neutral'}>{tool.access_mode}</Badge>
      </div>
      <div className="flex items-center gap-1">
        <Button variant="ghost" size="sm" onClick={onTest}>
          Test
        </Button>
        <Button variant="ghost" size="sm" onClick={onEdit}>
          Edit
        </Button>
        <Button variant="ghost" size="sm" onClick={unpublish} loading={busy} disabled={busy}>
          Remove
        </Button>
      </div>
    </li>
  );
}

// ── Publish / edit dialog ───────────────────────────────────────────────────────────────
interface ParamRow extends FlowToolParam {
  _id: number;
}

let _pid = 1;
function newParam(): ParamRow {
  return { _id: _pid++, name: '', type: 'string', required: false, description: '' };
}
function toRows(params: FlowToolParam[]): ParamRow[] {
  return params.map((p) => ({ ...p, _id: _pid++ }));
}

function PublishToolModal({
  open,
  editing,
  onClose,
  onPublished,
}: {
  open: boolean;
  editing: FlowTool | null;
  onClose: () => void;
  onPublished: () => void;
}) {
  const toast = useToast();
  const [flows, setFlows] = useState<NoderedFlow[]>([]);
  const [flowsError, setFlowsError] = useState<unknown>(null);
  const [flowId, setFlowId] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [accessMode, setAccessMode] = useState<AccessMode>('ask');
  const [params, setParams] = useState<ParamRow[]>([newParam()]);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  useEffect(() => {
    if (!open) return;
    // Pre-fill from the tool being edited, or reset to blank for a fresh publish.
    if (editing) {
      setTitle(editing.display_name);
      setDescription(editing.description);
      setAccessMode(editing.access_mode);
      const rows = paramsFromSchema(editing.input_schema);
      setParams(rows.length ? toRows(rows) : [newParam()]);
      setFlowId(editing.node_red_flow_id ?? '');
    } else {
      setTitle('');
      setDescription('');
      setAccessMode('ask');
      setParams([newParam()]);
      setFlowId('');
    }
    setFormError(null);
    setFlowsError(null);

    let cancelled = false;
    listNoderedFlows()
      .then((f) => {
        if (cancelled) return;
        setFlows(f);
        setFlowId((cur) => cur || (editing?.node_red_flow_id ?? '') || (f[0]?.id ?? ''));
      })
      .catch((e) => !cancelled && setFlowsError(e));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);

  function close() {
    if (submitting) return;
    onClose();
  }

  function updateParam(id: number, patch: Partial<ParamRow>) {
    setParams((ps) => ps.map((p) => (p._id === id ? { ...p, ...patch } : p)));
  }

  async function submit() {
    setFormError(null);
    if (!flowId) {
      setFormError(new Error('Select a workflow to publish.'));
      return;
    }
    if (!title.trim() || !description.trim()) {
      setFormError(new Error('A tool name and description are required.'));
      return;
    }
    const input_params: FlowToolParam[] = params
      .filter((p) => p.name.trim())
      .map((p) => ({
        name: p.name.trim(),
        type: p.type,
        required: p.required,
        description: p.description?.trim() || undefined,
      }));

    const body: PublishFlowToolRequest = {
      node_red_flow_id: flowId,
      tool: { title: title.trim(), description: description.trim(), access_mode: accessMode, input_params },
    };

    setSubmitting(true);
    try {
      const res = await publishFlowTool(body);
      toast.success(`${res.is_update ? 'Updated' : 'Published'} ${res.tool_name} (v${res.version}).`);
      onPublished();
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
      title={editing ? 'Edit Tool' : 'Publish Tool'}
      description="Turn the workflow you built into an MCP tool. No JSON required."
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} loading={submitting} disabled={submitting}>
            {editing ? 'Update Tool' : 'Publish Tool'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {formError ? <ErrorBanner error={formError} title="Could not publish" /> : null}
        {flowsError ? <ErrorBanner error={flowsError} title="Could not list your workflows" /> : null}

        <Select
          label="Workflow"
          value={flowId}
          onChange={(e) => setFlowId(e.target.value)}
          hint="Deploy the workflow in the editor first so it appears here."
        >
          {flows.length === 0 ? <option value="">No deployed workflows found</option> : null}
          {flows.map((f) => (
            <option key={f.id} value={f.id}>
              {f.label}
            </option>
          ))}
        </Select>

        <Input
          label="Tool name"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Sync Invoices"
          hint="A friendly name. The MCP tool name is derived from this (e.g. sync_invoices)."
        />
        <Textarea
          label="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          placeholder="What the tool does — the agent reads this to decide when to call it."
          className="font-sans"
        />

        <Select
          label="Access for agents"
          value={accessMode}
          onChange={(e) => setAccessMode(e.target.value as AccessMode)}
          hint={ACCESS_HINT[accessMode]}
        >
          <option value="ask">Ask (human approval per call)</option>
          <option value="none">Restricted (grant per agent later)</option>
          <option value="automated">Automated (no approval)</option>
        </Select>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <label className="text-sm font-medium text-fg">Input parameters</label>
            <Button variant="ghost" size="sm" onClick={() => setParams((ps) => [...ps, newParam()])}>
              + Add
            </Button>
          </div>
          <p className="text-xs text-muted">
            The typed inputs the agent must send. These become the tool&apos;s JSON Schema and are
            validated on every call.
          </p>
          {params.map((p) => (
            <div key={p._id} className="flex items-end gap-2">
              <Input
                className="flex-1"
                placeholder="param_name"
                value={p.name}
                onChange={(e) => updateParam(p._id, { name: e.target.value })}
                aria-label="Parameter name"
              />
              <Select
                className="w-28"
                value={p.type}
                onChange={(e) => updateParam(p._id, { type: e.target.value as FlowToolParamType })}
                aria-label="Parameter type"
              >
                <option value="string">string</option>
                <option value="integer">integer</option>
                <option value="number">number</option>
                <option value="boolean">boolean</option>
              </Select>
              <Select
                className="w-28"
                value={p.required ? 'yes' : 'no'}
                onChange={(e) => updateParam(p._id, { required: e.target.value === 'yes' })}
                aria-label="Required"
              >
                <option value="no">optional</option>
                <option value="yes">required</option>
              </Select>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setParams((ps) => (ps.length > 1 ? ps.filter((x) => x._id !== p._id) : ps))}
                aria-label="Remove parameter"
              >
                ✕
              </Button>
            </div>
          ))}
        </div>
      </div>
    </Modal>
  );
}

// ── Test dialog: run the tool with sample args ──────────────────────────────────────────
function TestToolModal({ tool, onClose }: { tool: FlowTool | null; onClose: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);

  const props = (tool?.input_schema?.properties ?? {}) as Record<string, { type?: string }>;
  const required = new Set((tool?.input_schema?.required as string[] | undefined) ?? []);
  const names = Object.keys(props);

  useEffect(() => {
    setValues({});
    setResult(null);
    setError(null);
  }, [tool]);

  function coerce(name: string, raw: string): unknown {
    const t = clampType(props[name]?.type);
    if (raw === '') return undefined;
    if (t === 'integer' || t === 'number') return Number(raw);
    if (t === 'boolean') return raw === 'true';
    return raw;
  }

  async function run() {
    if (!tool) return;
    setError(null);
    setResult(null);
    const args: Record<string, unknown> = {};
    for (const n of names) {
      const v = coerce(n, values[n] ?? '');
      if (v !== undefined) args[n] = v;
    }
    setRunning(true);
    try {
      const res = await testFlowTool(tool.slug, args);
      setResult(res.result);
    } catch (err) {
      setError(err);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Modal
      open={tool !== null}
      onClose={() => (running ? undefined : onClose())}
      title={tool ? `Test: ${tool.display_name}` : 'Test'}
      description="Run the workflow with sample inputs to confirm it works before an agent uses it."
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={running}>
            Close
          </Button>
          <Button onClick={run} loading={running} disabled={running}>
            Run tool
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {error ? <ErrorBanner error={error} title="Run failed" /> : null}
        {names.length === 0 ? (
          <p className="text-sm text-muted">This tool takes no inputs — just run it.</p>
        ) : (
          names.map((n) => {
            const t = clampType(props[n]?.type);
            return t === 'boolean' ? (
              <Select
                key={n}
                label={`${n}${required.has(n) ? ' *' : ''}`}
                value={values[n] ?? 'false'}
                onChange={(e) => setValues((v) => ({ ...v, [n]: e.target.value }))}
              >
                <option value="false">false</option>
                <option value="true">true</option>
              </Select>
            ) : (
              <Input
                key={n}
                label={`${n}${required.has(n) ? ' *' : ''}`}
                type={t === 'integer' || t === 'number' ? 'number' : 'text'}
                value={values[n] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [n]: e.target.value }))}
                hint={`type: ${t}`}
              />
            );
          })
        )}

        {result !== null ? (
          <div className="flex flex-col gap-1">
            <label className="text-sm font-medium text-fg">Result</label>
            <pre className="max-h-64 overflow-auto rounded-md border border-border bg-surface-2 p-3 text-xs">
              {JSON.stringify(result, null, 2)}
            </pre>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
