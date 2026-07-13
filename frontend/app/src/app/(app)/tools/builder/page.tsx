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
  ConfirmDialog,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  Select,
  Textarea,
  useToast,
} from '@/components/ui';
import {
  createBridgeTool,
  listBridgeTools,
  listMcps,
  listNoderedFlows,
  openEditorSession,
  testFlowTool,
  unpublishMcp,
} from '@/lib/services';
import type {
  AccessMode,
  BridgeTool,
  CreateBridgeToolRequest,
  CreateBridgeToolResult,
  FlowToolParam,
  FlowToolParamType,
  Mcp,
  NoderedFlow,
  TenantVisibility,
  ToolMcpMembership,
  ToolVisibility,
} from '@/lib/types';
import { McpMembershipTags, VisibilityBadge } from '@/components/Marketplace';
import { McpManagerCard } from '@/components/McpManager';
import { useAsync } from '@/lib/useAsync';

const ACCESS_HINT: Record<AccessMode, string> = {
  ask: 'Agents must get human approval on each call (recommended).',
  none: 'No agent can call it until a tenant admin grants access.',
  automated: 'Any agent can call it immediately — highest risk.',
};

// Only `private`/`protected` are self-declarable on publish; `public` is reached solely via
// admin promotion (the registry 400s a tenant-declared public), so it is never offered here.
const VISIBILITY_HINT: Record<TenantVisibility, string> = {
  private: 'Only your tenant can discover and attach this tool.',
  protected: 'Your tenant plus explicit grants (grant management is coming soon; today it behaves like private).',
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
  // One refresh token drives every list on this page: a publish creates/updates a tool AND its
  // MCP membership, and an MCP mutation can retire tools — so any change reloads both the
  // published-tools rail and the MCP-management panel.
  const [refreshToken, setRefreshToken] = useState(0);
  const refreshAll = useCallback(() => setRefreshToken((n) => n + 1), []);
  const tools = useAsync((signal) => listBridgeTools(signal), [refreshToken]);
  const [publishOpen, setPublishOpen] = useState(false);
  const [editing, setEditing] = useState<BridgeTool | null>(null);
  const [testing, setTesting] = useState<BridgeTool | null>(null);
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

  // Cold start: a per-tenant Node-RED runtime may still be provisioning (ready:false, e.g. a
  // scale-from-zero pod). Re-poll the editor session every few seconds until it reports ready,
  // so the iframe mounts as soon as the runtime comes up instead of sticking on the spinner.
  const reloadSession = session.reload;
  useEffect(() => {
    if (session.loading || session.error || ready) return;
    const t = setTimeout(reloadSession, 2500);
    return () => clearTimeout(t);
  }, [ready, session.loading, session.error, session.data, reloadSession]);

  function openPublish() {
    setEditing(null);
    setPublishOpen(true);
  }
  function openEdit(tool: BridgeTool) {
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
              <Loading
                label={
                  session.data && !ready
                    ? `Starting your workspace… (${session.data.runtime_status})`
                    : 'Preparing your workspace…'
                }
              />
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
                        key={t.tool_id}
                        tool={t}
                        onChanged={refreshAll}
                        onEdit={() => openEdit(t)}
                        onTest={() => setTesting(t)}
                      />
                    ))}
                  </ul>
                )}
              </CardBody>
            </Card>

            {/* ── MCP servers (aggregating collections) ──────────────────────── */}
            <McpManagerCard refreshToken={refreshToken} onChanged={refreshAll} />
          </aside>
        </div>
      </PageBody>

      <PublishToolModal
        open={publishOpen}
        editing={editing}
        onClose={() => setPublishOpen(false)}
        onPublished={() => {
          setPublishOpen(false);
          refreshAll();
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
  tool: BridgeTool;
  onChanged: () => void;
  onEdit: () => void;
  onTest: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // The new tool-view has no top-level slug/server_name — derive them from the tool's home MCP
  // membership (the first is its auto-singleton, `tool-<slug>`, unless it lives only in shared MCPs).
  const home: ToolMcpMembership | undefined = tool.mcps[0];

  async function unpublish() {
    if (!home) return;
    setBusy(true);
    try {
      await unpublishMcp(home.mcp_id);
      toast.success(`Unpublished ${tool.display_name}.`);
      setConfirmOpen(false);
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
          {home ? (
            <Link
              href={`/tools/${encodeURIComponent(home.server_name)}`}
              className="block truncate text-sm font-medium text-fg hover:text-brand hover:underline"
            >
              {tool.display_name}
            </Link>
          ) : (
            <span className="block truncate text-sm font-medium text-fg">{tool.display_name}</span>
          )}
          <p className="mt-0.5 truncate text-xs text-muted">
            <span className="font-mono">{tool.snake_name}</span> · v{tool.version}
          </p>
        </div>
        {tool.access_mode ? (
          <Badge tone={tool.access_mode === 'automated' ? 'warning' : 'neutral'}>{tool.access_mode}</Badge>
        ) : null}
      </div>
      <div className="flex items-center gap-1">
        <Button variant="ghost" size="sm" onClick={onTest} disabled={!home}>
          Test
        </Button>
        <Button variant="ghost" size="sm" onClick={onEdit}>
          Edit
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setConfirmOpen(true)} disabled={busy || !home}>
          Remove
        </Button>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        onClose={() => (busy ? undefined : setConfirmOpen(false))}
        onConfirm={unpublish}
        title="Unpublish this tool?"
        description="Agents will no longer discover or be able to call it. The workflow stays in the editor — you can re-publish it later."
        confirmLabel="Unpublish"
        loading={busy}
      >
        <p className="text-sm text-muted">
          <span className="font-medium text-fg">{tool.display_name}</span>{' '}
          <span className="font-mono text-xs">({tool.snake_name})</span>
        </p>
      </ConfirmDialog>
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

/** A JSON-Schema property name must be a valid identifier. */
const PARAM_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Reject malformed or duplicate parameter names before they become a wrong JSON Schema. */
function validateParamNames(rows: ParamRow[], kind: string): string | null {
  const seen = new Set<string>();
  for (const p of rows) {
    const n = p.name.trim();
    if (!n) continue;
    if (!PARAM_NAME_RE.test(n)) {
      return `${kind} parameter "${n}" is invalid — use letters, numbers and underscores, starting with a letter or underscore.`;
    }
    if (seen.has(n)) return `Duplicate ${kind} parameter "${n}". Each name must be unique.`;
    seen.add(n);
  }
  return null;
}

/** Reusable editor for a list of typed parameter rows (used for both inputs and outputs). */
function ParamRowsEditor({
  rows,
  setRows,
  label,
  hint,
  emptyHint,
  showRequired = true,
}: {
  rows: ParamRow[];
  setRows: React.Dispatch<React.SetStateAction<ParamRow[]>>;
  label: string;
  hint: string;
  emptyHint: string;
  showRequired?: boolean;
}) {
  const update = (id: number, patch: Partial<ParamRow>) =>
    setRows((ps) => ps.map((p) => (p._id === id ? { ...p, ...patch } : p)));
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <label className="text-sm font-medium text-fg">{label}</label>
        <Button variant="ghost" size="sm" onClick={() => setRows((ps) => [...ps, newParam()])}>
          + Add
        </Button>
      </div>
      <p className="text-xs text-muted">{hint}</p>
      {rows.length === 0 ? (
        <p className="text-xs text-faint">{emptyHint}</p>
      ) : (
        rows.map((p) => (
          <div key={p._id} className="flex items-end gap-2">
            <Input
              className="flex-1"
              placeholder="param_name"
              value={p.name}
              onChange={(e) => update(p._id, { name: e.target.value })}
              aria-label="Parameter name"
            />
            <Select
              className="w-28"
              value={p.type}
              onChange={(e) => update(p._id, { type: e.target.value as FlowToolParamType })}
              aria-label="Parameter type"
            >
              <option value="string">string</option>
              <option value="integer">integer</option>
              <option value="number">number</option>
              <option value="boolean">boolean</option>
            </Select>
            {showRequired ? (
              <Select
                className="w-28"
                value={p.required ? 'yes' : 'no'}
                onChange={(e) => update(p._id, { required: e.target.value === 'yes' })}
                aria-label="Required"
              >
                <option value="no">optional</option>
                <option value="yes">required</option>
              </Select>
            ) : null}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setRows((ps) => ps.filter((x) => x._id !== p._id))}
              aria-label="Remove parameter"
            >
              ✕
            </Button>
          </div>
        ))
      )}
    </div>
  );
}

/** The Visibility control only offers `private`/`protected` — clamp an edited tool's stored
 *  visibility to what the control can represent (a `public` tool falls back to `private`, but its
 *  self-declared visibility is never silently downgraded from `protected`). */
function toTenantVisibility(v: ToolVisibility | null | undefined): TenantVisibility {
  return v === 'protected' ? 'protected' : 'private';
}

/** An MCP membership is the tool's auto-created singleton (`tool-<slug>`) rather than a shared MCP. */
function isAutoSingleton(m: ToolMcpMembership): boolean {
  return m.server_name.startsWith('tool-');
}

function PublishToolModal({
  open,
  editing,
  onClose,
  onPublished,
}: {
  open: boolean;
  editing: BridgeTool | null;
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
  const [visibility, setVisibility] = useState<TenantVisibility>('private');
  const [params, setParams] = useState<ParamRow[]>([newParam()]);
  const [outputParams, setOutputParams] = useState<ParamRow[]>([]);
  // MCP assignment: the existing MCP(s) this tool joins. Empty ⇒ "standalone" (the bridge
  // auto-creates a singleton MCP, `tool-<slug>`). A tool can belong to MANY MCPs.
  const [mcps, setMcps] = useState<Mcp[]>([]);
  const [mcpsError, setMcpsError] = useState<unknown>(null);
  const [selectedMcpIds, setSelectedMcpIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);
  // The create result — shown as a success summary (server_name/mcp_slug + visibility) instead of
  // closing immediately, so the user sees where their tool landed.
  const [result, setResult] = useState<CreateBridgeToolResult | null>(null);

  useEffect(() => {
    if (!open) return;
    // Pre-fill from the tool being edited, or reset to blank for a fresh publish.
    if (editing) {
      setTitle(editing.display_name);
      setDescription(editing.description);
      setAccessMode(editing.access_mode ?? 'ask');
      const rows = paramsFromSchema(editing.input_schema);
      setParams(rows.length ? toRows(rows) : [newParam()]);
      setOutputParams(toRows(paramsFromSchema(editing.output_schema ?? undefined)));
      setFlowId(editing.node_red_flow_id ?? '');
      // Seed the Visibility control from the tool so a re-publish never silently downgrades it.
      setVisibility(toTenantVisibility(editing.visibility));
      // Seed the MCP memberships so re-publishing keeps the tool in its current MCP(s) instead of
      // spawning a fresh auto-singleton. But if its ONLY home is that auto-singleton, keep the
      // standalone path (no mcp_ids) so we don't force the shared-membership branch.
      const memberships = editing.mcps ?? [];
      const onlyStandalone = memberships.length === 1 && isAutoSingleton(memberships[0]);
      setSelectedMcpIds(onlyStandalone ? [] : memberships.map((m) => m.mcp_id));
    } else {
      setTitle('');
      setDescription('');
      setAccessMode('ask');
      setParams([newParam()]);
      setOutputParams([]);
      setFlowId('');
      setVisibility('private');
      setSelectedMcpIds([]);
    }
    setResult(null);
    setFormError(null);
    setFlowsError(null);
    setMcpsError(null);

    let cancelled = false;
    listNoderedFlows()
      .then((f) => {
        if (cancelled) return;
        setFlows(f);
        setFlowId((cur) => cur || (editing?.node_red_flow_id ?? '') || (f[0]?.id ?? ''));
      })
      .catch((e) => !cancelled && setFlowsError(e));
    // Populate the MCP-assignment picker. A failure is non-blocking — the user can still publish
    // standalone — so it surfaces as a soft note rather than an error banner.
    listMcps()
      .then((m) => !cancelled && setMcps(m))
      .catch((e) => !cancelled && setMcpsError(e));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);

  function close() {
    if (submitting) return;
    // After a successful publish, closing should refresh the page lists (onPublished closes + reloads).
    if (result) onPublished();
    else onClose();
  }

  function toggleMcp(id: string) {
    setSelectedMcpIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  }

  function toParams(rows: ParamRow[]): FlowToolParam[] {
    return rows
      .filter((p) => p.name.trim())
      .map((p) => ({
        name: p.name.trim(),
        type: p.type,
        required: p.required,
        description: p.description?.trim() || undefined,
      }));
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
    const nameError = validateParamNames(params, 'Input') || validateParamNames(outputParams, 'Output');
    if (nameError) {
      setFormError(new Error(nameError));
      return;
    }

    const input_params = toParams(params);
    const output_params = toParams(outputParams);

    // Source-of-truth path (spec Phase 2/4B): create the atomic tool + its MCP membership.
    // `visibility` is private|protected only; empty `mcp_ids` ⇒ auto-singleton MCP.
    const body: CreateBridgeToolRequest = {
      node_red_flow_id: flowId,
      title: title.trim(),
      description: description.trim(),
      access_mode: accessMode,
      visibility,
      input_params,
      ...(output_params.length ? { output_params } : {}),
      ...(selectedMcpIds.length ? { mcp_ids: selectedMcpIds } : {}),
    };

    setSubmitting(true);
    try {
      const res = await createBridgeTool(body);
      toast.success(`${res.is_update ? 'Updated' : 'Published'} ${res.snake_name} (v${res.version}).`);
      setResult(res);
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
      title={result ? 'Tool published' : editing ? 'Edit Tool' : 'Publish Tool'}
      description="Turn the workflow you built into an MCP tool. No JSON required."
      size="lg"
      footer={
        result ? (
          <Button onClick={onPublished}>Done</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={close} disabled={submitting}>
              Cancel
            </Button>
            <Button onClick={submit} loading={submitting} disabled={submitting}>
              {editing ? 'Update Tool' : 'Publish Tool'}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <PublishResult result={result} />
      ) : (
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

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
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

            <Select
              label="Visibility"
              value={visibility}
              onChange={(e) => setVisibility(e.target.value as TenantVisibility)}
              hint={VISIBILITY_HINT[visibility]}
            >
              <option value="private">Private — only your tenant</option>
              <option value="protected">Protected — your tenant + grants (soon)</option>
            </Select>
          </div>

          <McpAssignment
            mcps={mcps}
            mcpsError={mcpsError}
            selectedIds={selectedMcpIds}
            onToggle={toggleMcp}
          />

          <ParamRowsEditor
            rows={params}
            setRows={setParams}
            label="Input parameters"
            hint="The typed inputs the agent must send. These become the tool's input JSON Schema and are validated on every call."
            emptyHint="No inputs — the agent calls this tool with no arguments."
          />

          <ParamRowsEditor
            rows={outputParams}
            setRows={setOutputParams}
            label="Output parameters"
            hint="Optional: the typed fields the tool returns, so the agent knows the result shape. Leave empty to return an untyped result."
            emptyHint="No declared outputs — the tool's result is returned untyped."
            showRequired={false}
          />
        </div>
      )}
    </Modal>
  );
}

/** MCP-assignment control: pick the existing MCP(s) to join, or none for a standalone singleton. */
function McpAssignment({
  mcps,
  mcpsError,
  selectedIds,
  onToggle,
}: {
  mcps: Mcp[];
  mcpsError: unknown;
  selectedIds: string[];
  onToggle: (id: string) => void;
}) {
  const standalone = selectedIds.length === 0;
  return (
    <div className="flex flex-col gap-2">
      <label className="text-sm font-medium text-fg">MCP assignment</label>
      <p className="text-xs text-muted">
        Add this tool to one or more existing MCP servers, or leave all unchecked to publish it{' '}
        <strong>standalone</strong> — a dedicated MCP is created automatically.
      </p>
      {mcpsError ? (
        <p className="text-xs text-warning">
          Could not list your MCP servers — you can still publish standalone.
        </p>
      ) : mcps.length === 0 ? (
        <p className="text-xs text-faint">
          No MCP servers yet. This tool will be published standalone.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
          {mcps.map((m) => {
            const checked = selectedIds.includes(m.mcp_id);
            return (
              <li key={m.mcp_id}>
                <label className="flex cursor-pointer items-start gap-2.5 px-3 py-2 hover:bg-surface-2">
                  <input
                    type="checkbox"
                    className="mt-1 shrink-0 accent-brand"
                    checked={checked}
                    onChange={() => onToggle(m.mcp_id)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium text-fg">{m.display_name}</span>
                      <VisibilityBadge visibility={m.visibility} />
                    </span>
                    <span className="mt-0.5 block truncate font-mono text-xs text-muted">
                      {m.server_name}
                    </span>
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
      <p className="text-xs text-faint">
        {standalone ? 'Will be published standalone (auto-singleton MCP).' : `Joining ${selectedIds.length} MCP(s).`}
      </p>
    </div>
  );
}

/** Success summary after a publish — where the tool landed (server + slug + visibility + MCPs). */
function PublishResult({ result }: { result: CreateBridgeToolResult }) {
  return (
    <div className="flex flex-col gap-3">
      <Callout tone="success" title={`${result.is_update ? 'Updated' : 'Published'} ${result.display_name}`}>
        Your workflow is live as an MCP tool. Agents attach the MCP server below to discover it.
      </Callout>
      <dl className="flex flex-col gap-2 text-sm">
        <div className="flex items-center justify-between gap-3">
          <dt className="text-muted">Tool name</dt>
          <dd className="truncate font-mono text-xs text-fg">{result.snake_name}</dd>
        </div>
        <div className="flex items-center justify-between gap-3">
          <dt className="text-muted">MCP server</dt>
          <dd className="truncate font-mono text-xs text-fg">{result.server_name ?? result.mcp_slug ?? '—'}</dd>
        </div>
        <div className="flex items-center justify-between gap-3">
          <dt className="text-muted">Visibility</dt>
          <dd>
            <VisibilityBadge visibility={result.visibility} />
          </dd>
        </div>
        <div className="flex items-start justify-between gap-3">
          <dt className="pt-0.5 text-muted">Member of</dt>
          <dd className="min-w-0">
            <McpMembershipTags memberships={result.mcps} />
          </dd>
        </div>
      </dl>
    </div>
  );
}

// ── Test dialog: run the tool with sample args ──────────────────────────────────────────
function TestToolModal({ tool, onClose }: { tool: BridgeTool | null; onClose: () => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);

  // The tool-invoke test endpoint keys off the flow-tool slug — read it from the home MCP membership.
  const home = tool?.mcps?.[0];
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
    if (!tool || !home) return;
    setError(null);
    setResult(null);
    const args: Record<string, unknown> = {};
    for (const n of names) {
      const v = coerce(n, values[n] ?? '');
      if (v !== undefined) args[n] = v;
    }
    setRunning(true);
    try {
      const res = await testFlowTool(home.slug, args);
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
          <Button onClick={run} loading={running} disabled={running || !home}>
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
