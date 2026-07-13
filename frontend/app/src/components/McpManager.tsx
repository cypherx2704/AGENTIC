'use client';

import { useEffect, useState } from 'react';
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
  Modal,
  Select,
  Textarea,
  useToast,
} from '@/components/ui';
import { McpMembershipTags, VisibilityBadge } from '@/components/Marketplace';
import { useSession } from '@/components/SessionProvider';
import {
  createMcp,
  listBridgeTools,
  listMcps,
  promoteMcp,
  publishMcp,
  unpublishMcp,
  updateMcp,
} from '@/lib/services';
import type {
  BridgeTool,
  CreateMcpRequest,
  Mcp,
  TenantVisibility,
  UpdateMcpRequest,
} from '@/lib/types';
import { useAsync } from '@/lib/useAsync';

/**
 * MCP-management panel for the Tool Builder (spec Phase 4B). Lists the tenant's MCP collections
 * (aggregating servers) with their members + visibility, and drives the flow-bridge control plane:
 * create (`createMcp`), edit metadata + membership (`updateMcp` — `tool_ids` REPLACES membership),
 * publish/re-register (`publishMcp`), and unpublish (`unpublishMcp`, which also retires the tools
 * that belong ONLY to this MCP). Member/tool pickers are populated from `listBridgeTools()`.
 *
 * Owned by 4B — deliberately named to avoid colliding with the shared Marketplace primitives it
 * reuses (`VisibilityBadge`, `McpMembershipTags`).
 */

const VISIBILITY_HINT: Record<TenantVisibility, string> = {
  private: 'Only your tenant can see and attach this MCP.',
  protected: 'Your tenant plus explicit grants (grant management is coming soon; today it behaves like private).',
};

/** Clamp a stored visibility to the two a tenant may self-declare (`public` is promote-only). */
function asTenantVisibility(v: string | null | undefined): TenantVisibility {
  return v === 'protected' ? 'protected' : 'private';
}

// ── Panel ─────────────────────────────────────────────────────────────────────────────────
export function McpManagerCard({
  refreshToken,
  onChanged,
}: {
  /** Bumped by the page after an external mutation (e.g. a publish) to force a reload. */
  refreshToken: number;
  /** Notify the page so sibling lists (published-tools rail) refresh after an MCP mutation. */
  onChanged: () => void;
}) {
  const mcps = useAsync((signal) => listMcps(signal), [refreshToken]);
  const [editing, setEditing] = useState<Mcp | null>(null);
  const [creating, setCreating] = useState(false);

  function reloadAll() {
    mcps.reload();
    onChanged();
  }

  return (
    <Card>
      <CardHeader
        title="MCP servers"
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={mcps.reload}>
              Refresh
            </Button>
            <Button size="sm" onClick={() => setCreating(true)}>
              New MCP
            </Button>
          </>
        }
      />
      <CardBody className="px-0 py-0">
        {mcps.error ? (
          <div className="p-4">
            <ErrorBanner error={mcps.error} title="Could not load MCP servers" />
          </div>
        ) : mcps.loading ? (
          <Loading label="Loading…" />
        ) : (mcps.data ?? []).length === 0 ? (
          <p className="p-4 text-sm text-muted">
            No MCP servers yet. Group tools into one with <strong>New MCP</strong>, or publish a tool
            standalone to get an auto-created MCP.
          </p>
        ) : (
          <ul className="flex flex-col">
            {(mcps.data ?? []).map((m) => (
              <McpRow key={m.mcp_id} mcp={m} onEdit={() => setEditing(m)} onChanged={reloadAll} />
            ))}
          </ul>
        )}
      </CardBody>

      <McpEditModal
        open={creating || editing !== null}
        editing={editing}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSaved={() => {
          setCreating(false);
          setEditing(null);
          reloadAll();
        }}
      />
    </Card>
  );
}

// ── Row ───────────────────────────────────────────────────────────────────────────────────
function McpRow({
  mcp,
  onEdit,
  onChanged,
}: {
  mcp: Mcp;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const { session } = useSession();
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const retired = mcp.status === 'retired';
  const isPublic = mcp.visibility === 'public';
  // Promote is the SOLE path to Public and is a platform-admin action — only show it to a session
  // that carries `platform:admin`, and never for a MCP that is already public / retired.
  const canPromote = (session?.scopes ?? []).includes('platform:admin') && !isPublic && !retired;

  async function publish() {
    setBusy(true);
    try {
      await publishMcp(mcp.mcp_id);
      toast.success(`Re-registered ${mcp.display_name}.`);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not publish.');
    } finally {
      setBusy(false);
    }
  }

  async function unpublish() {
    setBusy(true);
    try {
      const res = await unpublishMcp(mcp.mcp_id);
      const n = res.retired_tools?.length ?? 0;
      toast.success(
        `Unpublished ${mcp.display_name}${
          n ? ` — retired ${n} exclusively-owned tool${n === 1 ? '' : 's'}` : ''
        }.`,
      );
      setConfirmOpen(false);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not unpublish.');
    } finally {
      setBusy(false);
    }
  }

  async function promote() {
    setBusy(true);
    try {
      const res = await promoteMcp(mcp.mcp_id);
      const slug = res.server_name || res.slug;
      const rehomed = res.runtime_rehomed ? ' — flows re-homed to the platform runtime' : '';
      toast.success(`Promoted to Public as ${slug}${rehomed}.`);
      setPromoteOpen(false);
      onChanged();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not promote.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-col gap-1.5 border-t border-border p-3 first:border-t-0">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-fg">{mcp.display_name}</p>
          <p className="mt-0.5 truncate text-xs text-muted">
            <span className="font-mono">{mcp.server_name}</span> · v{mcp.version}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          <VisibilityBadge visibility={mcp.visibility} />
          {retired ? <Badge tone="danger">Retired</Badge> : null}
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-faint">
          {mcp.tools.length} {mcp.tools.length === 1 ? 'member' : 'members'}
        </span>
        {mcp.tools.length ? (
          <span className="inline-flex flex-wrap gap-1">
            {mcp.tools.map((t) => (
              <Badge key={t.tool_id} tone="neutral" className="font-mono">
                <span title={t.display_name}>{t.snake_name}</span>
              </Badge>
            ))}
          </span>
        ) : (
          <span className="text-xs text-faint">No members yet.</span>
        )}
      </div>

      <div className="flex items-center gap-1">
        <Button variant="ghost" size="sm" onClick={onEdit} disabled={busy}>
          Edit
        </Button>
        <Button variant="ghost" size="sm" onClick={publish} disabled={busy || retired}>
          Publish
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setConfirmOpen(true)} disabled={busy || retired}>
          Unpublish
        </Button>
        {canPromote ? (
          <Button variant="ghost" size="sm" onClick={() => setPromoteOpen(true)} disabled={busy}>
            Promote to Public
          </Button>
        ) : null}
      </div>

      <ConfirmDialog
        open={confirmOpen}
        onClose={() => (busy ? undefined : setConfirmOpen(false))}
        onConfirm={unpublish}
        title="Unpublish this MCP?"
        description="The MCP server is retired and agents can no longer attach it. Tools that belong ONLY to this MCP are retired too; tools shared with another MCP stay live."
        confirmLabel="Unpublish"
        loading={busy}
      >
        <p className="text-sm text-muted">
          <span className="font-medium text-fg">{mcp.display_name}</span>{' '}
          <span className="font-mono text-xs">({mcp.server_name})</span>
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={promoteOpen}
        onClose={() => (busy ? undefined : setPromoteOpen(false))}
        onConfirm={promote}
        title="Promote this MCP to Public?"
        description="This re-homes the MCP's flows to the platform runtime and makes it public to ALL tenants. Public is reached only through this action, and it cannot be undone from here."
        confirmLabel="Promote to Public"
        confirmVariant="primary"
        loading={busy}
      >
        <p className="text-sm text-muted">
          <span className="font-medium text-fg">{mcp.display_name}</span>{' '}
          <span className="font-mono text-xs">({mcp.server_name})</span>
        </p>
      </ConfirmDialog>
    </li>
  );
}

// ── Create / edit dialog ────────────────────────────────────────────────────────────────────
function McpEditModal({
  open,
  editing,
  onClose,
  onSaved,
}: {
  open: boolean;
  editing: Mcp | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [visibility, setVisibility] = useState<TenantVisibility>('private');
  const [selectedToolIds, setSelectedToolIds] = useState<string[]>([]);
  const [tools, setTools] = useState<BridgeTool[]>([]);
  const [toolsError, setToolsError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<unknown>(null);

  useEffect(() => {
    if (!open) return;
    if (editing) {
      setDisplayName(editing.display_name);
      setDescription(editing.description);
      setVisibility(asTenantVisibility(editing.visibility));
      setSelectedToolIds(editing.tools.map((t) => t.tool_id));
    } else {
      setDisplayName('');
      setDescription('');
      setVisibility('private');
      setSelectedToolIds([]);
    }
    setFormError(null);
    setToolsError(null);

    let cancelled = false;
    listBridgeTools()
      .then((t) => !cancelled && setTools(t))
      .catch((e) => !cancelled && setToolsError(e));
    return () => {
      cancelled = true;
    };
  }, [open, editing]);

  function toggleTool(id: string) {
    setSelectedToolIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  }

  function close() {
    if (!submitting) onClose();
  }

  async function submit() {
    setFormError(null);
    if (!displayName.trim() || !description.trim()) {
      setFormError(new Error('An MCP name and description are required.'));
      return;
    }
    setSubmitting(true);
    try {
      if (editing) {
        // `tool_ids` REPLACES the whole membership set (add/remove in one PUT).
        const body: UpdateMcpRequest = {
          display_name: displayName.trim(),
          description: description.trim(),
          visibility,
          tool_ids: selectedToolIds,
        };
        const res = await updateMcp(editing.mcp_id, body);
        toast.success(`Updated ${res.display_name} (${res.tools.length} tools).`);
      } else {
        const body: CreateMcpRequest = {
          display_name: displayName.trim(),
          description: description.trim(),
          visibility,
          ...(selectedToolIds.length ? { tool_ids: selectedToolIds } : {}),
        };
        const res = await createMcp(body);
        toast.success(`Created ${res.server_name}.`);
      }
      onSaved();
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
      title={editing ? 'Edit MCP' : 'New MCP'}
      description="An MCP is a named collection of tools registered as one server. Agents attach the server and get all its member tools."
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} loading={submitting} disabled={submitting}>
            {editing ? 'Save changes' : 'Create MCP'}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {formError ? <ErrorBanner error={formError} title="Could not save the MCP" /> : null}

        <Input
          label="MCP name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="Billing tools"
          hint="A friendly name. The server slug is derived from this (e.g. mcp-billing-tools-1a2b3c4d)."
        />
        <Textarea
          label="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          placeholder="What this collection is for — helps agents decide when to attach it."
          className="font-sans"
        />

        <Select
          label="Visibility"
          value={visibility}
          onChange={(e) => setVisibility(e.target.value as TenantVisibility)}
          hint={VISIBILITY_HINT[visibility]}
        >
          <option value="private">Private — only your tenant</option>
          <option value="protected">Protected — your tenant + grants (soon)</option>
        </Select>

        <div className="flex flex-col gap-2">
          <label className="text-sm font-medium text-fg">Member tools</label>
          <p className="text-xs text-muted">
            {editing
              ? 'Check the tools this MCP aggregates — this replaces the current membership, so unchecking removes a tool.'
              : 'Check the tools this MCP aggregates. Leave empty to create it now and add tools later.'}
          </p>
          {toolsError ? (
            <ErrorBanner error={toolsError} title="Could not list your tools" />
          ) : tools.length === 0 ? (
            <p className="text-xs text-faint">
              No atomic tools yet. Publish a tool first, then add it here.
            </p>
          ) : (
            <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
              {tools.map((t) => {
                const checked = selectedToolIds.includes(t.tool_id);
                return (
                  <li key={t.tool_id}>
                    <label className="flex cursor-pointer items-start gap-2.5 px-3 py-2 hover:bg-surface-2">
                      <input
                        type="checkbox"
                        className="mt-1 shrink-0 accent-brand"
                        checked={checked}
                        onChange={() => toggleTool(t.tool_id)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium text-fg">
                            {t.display_name || t.snake_name}
                          </span>
                          <VisibilityBadge visibility={t.visibility} />
                        </span>
                        <span className="mt-0.5 block truncate font-mono text-xs text-muted">
                          {t.snake_name}
                        </span>
                        <span className="mt-1 block">
                          <McpMembershipTags memberships={t.mcps} />
                        </span>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </Modal>
  );
}
