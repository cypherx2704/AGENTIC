'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  OwnerBadge,
  VisibilityBadge,
  capabilityNames,
  cleanMcpName,
  descriptionOf,
} from '@/components/Marketplace';
import { MarketplaceBrowser, type BrowserMcp, type BrowserTool } from '@/components/MarketplaceBrowser';
import { Button, Modal, useToast } from '@/components/ui';
import { getToolAccess, listMcps, listTools } from '@/lib/services';
import { seedMcpMemberAccess, seedToolMemberAccess } from '@/lib/agentTools';
import { useAsync } from '@/lib/useAsync';
import type { AccessMode, Mcp, ToolView } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * AgentToolPicker — the Marketplace-style tool selector that replaces the comma-separated
 * `allowed_tools` field (Phase 4C). It writes the platform's TWO tool stores:
 *
 *  1. `allowed_tools` (xAgent runtime) — a list of MCP SERVER names. This is the picker's
 *     `servers`/`onServersChange` controlled value; the parent persists it via `putRuntime`.
 *  2. `agent_tool_access` (tool-registry) — a per-(agent, server_name, capability) grant of
 *     `automated` (ALLOWED) or `none` (GREYED). Set via `setToolAccess(server, {capability, …})`.
 *
 * Add an MCP  → its `server_name` joins `allowed_tools`; each member is seeded from its DEFAULT
 *               access (spec A1): `automated` members come in ALLOWED, restricted (`ask`/`none`)
 *               members come in GREYED so nothing falls back to a permissive registry default.
 * Add a tool  → its containing MCP `server_name` joins `allowed_tools` and ONLY that capability is
 *               ALLOWED; its siblings stay GREYED (none).
 * The `✕` on an allowed member toggles it to `none` (and back).
 *
 * The "Add" flow is the FULL Marketplace popup ({@link MarketplaceBrowser}) — the same 3-tab ×
 * 2-section catalogue the standalone Marketplace page shows (spec A4).
 *
 * Two modes (mirroring the old ToolAccessSection):
 *  - LIVE (`agentId` set): every grant persists immediately via `setToolAccess`.
 *  - DEFERRED (`onGrantsChange`): grants are collected and emitted for the parent to apply after
 *    the agent (and its runtime) exist — used by the Create Agent modal.
 */

/**
 * A collected per-capability grant staged for apply-after-save. It carries the FULL desired mode —
 * `automated` for allowed members and explicit `none` for greyed siblings — so the apply step
 * commits siblings explicitly instead of letting them fall back to the registry's permissive
 * `default_access_mode`.
 */
export interface AgentToolGrant {
  server_name: string;
  capability: string;
  access_mode: AccessMode;
}

/** A normalized MCP server the picker holds member metadata for (attached-panel chips + hydration). */
interface PickerMcp {
  server_name: string;
  display_name: string;
  description?: string;
  visibility?: string | null;
  isPlatform: boolean;
  members: Array<{ capability: string; display_name: string; access_mode?: AccessMode | null }>;
}

// ── key helpers (server_name + capability -> a single map key) ────────────────────────────
const SEP = ' ';
const keyOf = (server: string, capability: string) => `${server}${SEP}${capability}`;
function splitKey(k: string): { server_name: string; capability: string } {
  const i = k.indexOf(SEP);
  return { server_name: k.slice(0, i), capability: k.slice(i + 1) };
}

// ── data loading (member metadata for the attached panel: tenant MCPs + public platform servers) ──
function mcpFromTenant(m: Mcp): PickerMcp {
  return {
    server_name: m.server_name,
    display_name: m.display_name || cleanMcpName(m.server_name),
    description: m.description || undefined,
    visibility: m.visibility,
    isPlatform: false,
    members: (m.tools ?? []).map((t) => ({
      capability: t.snake_name,
      display_name: t.display_name || t.snake_name,
      access_mode: t.access_mode ?? null,
    })),
  };
}

function mcpFromRegistry(s: ToolView): PickerMcp {
  return {
    server_name: s.name,
    display_name: cleanMcpName(s.name),
    description: descriptionOf(s),
    visibility: (s.visibility as string) ?? 'public',
    isPlatform: s.is_platform ?? true,
    members: capabilityNames(s).map((n) => ({ capability: n, display_name: n, access_mode: null })),
  };
}

async function loadPickerData(signal: AbortSignal): Promise<PickerMcp[]> {
  const [mcpsR, publicR] = await Promise.allSettled([listMcps(signal), listTools({ visibility: 'public' }, signal)]);
  const mcps: PickerMcp[] = [];
  const seen = new Set<string>();
  if (mcpsR.status === 'fulfilled') {
    for (const m of mcpsR.value) {
      if (seen.has(m.server_name)) continue;
      seen.add(m.server_name);
      mcps.push(mcpFromTenant(m));
    }
  }
  if (publicR.status === 'fulfilled') {
    for (const s of publicR.value) {
      if (!s.name || seen.has(s.name)) continue;
      seen.add(s.name);
      mcps.push(mcpFromRegistry(s));
    }
  }
  return mcps;
}

// ── component ─────────────────────────────────────────────────────────────────────────────
export interface AgentToolPickerProps {
  /** `allowed_tools` — the attached MCP server names. Controlled by the parent (persisted via the runtime). */
  servers: string[];
  onServersChange: (next: string[]) => void;
  /** LIVE mode: when set, per-capability grants persist immediately for this agent. */
  agentId?: string;
  /** DEFERRED mode: emit the collected `automated` grants for the parent to apply after createAgent. */
  onGrantsChange?: (grants: AgentToolGrant[]) => void;
}

export function AgentToolPicker({ servers, onServersChange, agentId, onGrantsChange }: AgentToolPickerProps) {
  const toast = useToast();
  const dataQ = useAsync((signal) => loadPickerData(signal), []);
  const mcps = useMemo(() => dataQ.data ?? [], [dataQ.data]);
  const mcpByServer = useMemo(() => new Map(mcps.map((m) => [m.server_name, m])), [mcps]);

  // Per-(server, capability) access map. 'automated' = ALLOWED, anything else = GREYED. In BOTH
  // modes this map is STAGED (never written to the registry inline) — the parent applies it to the
  // tool-registry AFTER the runtime's `allowed_tools` is saved (create) / re-saved (edit), so the
  // two stores never diverge (no grant is written while `allowed_tools` still lags, and vice-versa).
  const [access, setAccess] = useState<Record<string, AccessMode>>({});
  const [browseOpen, setBrowseOpen] = useState(false);
  const [showRaw, setShowRaw] = useState(false);

  // LIVE mode hydrates the current grants for the already-attached servers; DEFERRED starts clean.
  const [hydrated, setHydrated] = useState<boolean>(!agentId);
  const hydratedRef = useRef(false);

  useEffect(() => {
    if (!agentId) return;
    if (hydratedRef.current || dataQ.loading) return;
    hydratedRef.current = true;
    const snapshot = servers;
    if (snapshot.length === 0) {
      setHydrated(true);
      return;
    }
    let cancelled = false;
    (async () => {
      const pairs: Array<[string, string]> = [];
      for (const server of snapshot) {
        for (const m of mcpByServer.get(server)?.members ?? []) pairs.push([server, m.capability]);
      }
      const entries = await Promise.all(
        pairs.map(async ([server, capability]) => {
          try {
            const a = await getToolAccess(server, { agent_id: agentId, capability });
            return [keyOf(server, capability), a.access_mode] as const;
          } catch {
            return [keyOf(server, capability), 'none' as AccessMode] as const;
          }
        }),
      );
      if (cancelled) return;
      setAccess((prev) => ({ ...Object.fromEntries(entries), ...prev }));
      setHydrated(true);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, dataQ.loading]);

  const isAllowed = (server: string, capability: string) => access[keyOf(server, capability)] === 'automated';

  /**
   * Emit the FULL staged access map — every (server, capability) with its mode, INCLUDING explicit
   * `none` for greyed siblings — so the parent commits siblings explicitly (never a silent fallback
   * to the registry default). Called only on a deliberate user edit, never on hydration, so loading
   * the current grants can't mark the parent form dirty.
   */
  function emitGrants(map: Record<string, AccessMode>) {
    onGrantsChange?.(Object.entries(map).map(([k, mode]) => ({ ...splitKey(k), access_mode: mode })));
  }

  /** Apply a new staged access map locally and emit it to the parent (a user edit). */
  function stageAccess(next: Record<string, AccessMode>) {
    setAccess(next);
    emitGrants(next);
  }

  /**
   * Add a whole MCP (spec A1): join `allowed_tools` and seed each member from its DEFAULT access —
   * `automated` members come in ALLOWED, restricted (`ask`/`none`) members come in GREYED. Members
   * come from the popup's marketplace item (which carries per-member `access_mode` for tenant MCPs;
   * public/platform servers expose no default, so those members come in allowed).
   */
  function addMcp(mcp: BrowserMcp) {
    const next = { ...access };
    const seeded = seedMcpMemberAccess(mcp.members);
    for (const [capability, mode] of Object.entries(seeded)) next[keyOf(mcp.server_name, capability)] = mode;
    stageAccess(next);
    if (!servers.includes(mcp.server_name)) onServersChange([...servers, mcp.server_name]);
  }

  function addTool(tool: BrowserTool) {
    const server = tool.server_name;
    if (!server) {
      toast.error('This tool has no MCP server yet — publish it to an MCP first.');
      return;
    }
    const alreadyAttached = servers.includes(server);
    const next = { ...access };
    if (!alreadyAttached) {
      // New server: allow only this capability, leave its siblings greyed (explicit none).
      const members = tool.members.length ? tool.members : [{ capability: tool.capability }];
      const seeded = seedToolMemberAccess(members, tool.capability);
      for (const [capability, mode] of Object.entries(seeded)) next[keyOf(server, capability)] = mode;
    } else {
      next[keyOf(server, tool.capability)] = 'automated';
    }
    stageAccess(next);
    if (!alreadyAttached) onServersChange([...servers, server]);
  }

  function removeServer(server: string) {
    // Mark this server's grants `none` (rather than dropping the keys) so the staged apply REVOKES
    // them together with the `allowed_tools` removal — otherwise a removed server would leave its
    // grant orphaned/live in the registry until some later, unrelated write.
    const next = { ...access };
    for (const k of Object.keys(next)) {
      if (splitKey(k).server_name === server) next[k] = 'none';
    }
    onServersChange(servers.filter((s) => s !== server));
    stageAccess(next);
  }

  function toggleMember(server: string, capability: string) {
    const k = keyOf(server, capability);
    const nextMode: AccessMode = isAllowed(server, capability) ? 'none' : 'automated';
    stageAccess({ ...access, [k]: nextMode });
  }

  return (
    <div className="flex flex-col gap-3" role="group" aria-label="Agent tools">
      <div>
        <p className="text-sm font-medium text-fg">Tools &amp; MCP servers</p>
        <p className="text-xs text-muted">
          Attach an MCP server (all its tools become available) or an individual tool. Each attached
          tool is <span className="text-fg">allowed</span> (automated) or <span className="text-fg">greyed</span>{' '}
          (denied) — click a tool to toggle it.
        </p>
      </div>

      {/* ── attached servers ─────────────────────────────────────────────────────────────── */}
      {servers.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted">
          No tools attached yet. Browse the marketplace to add an MCP server or an individual tool.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {servers.map((server) => {
            const mcp = mcpByServer.get(server);
            // Members to show: the MCP's own members, unioned with anything we hold access for.
            const caps = new Map<string, string>();
            for (const m of mcp?.members ?? []) caps.set(m.capability, m.display_name);
            for (const k of Object.keys(access)) {
              const s = splitKey(k);
              if (s.server_name === server && !caps.has(s.capability)) caps.set(s.capability, s.capability);
            }
            const members = [...caps.entries()];
            return (
              <div key={server} className="rounded-md border border-border bg-surface p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-fg-strong">
                      {mcp?.display_name ?? cleanMcpName(server)}
                    </p>
                    <p className="truncate font-mono text-xs text-muted">{server}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {mcp ? <VisibilityBadge visibility={mcp.visibility} /> : null}
                    {mcp ? <OwnerBadge isPlatform={mcp.isPlatform} /> : null}
                    <button
                      type="button"
                      onClick={() => removeServer(server)}
                      title="Remove this server from the agent"
                      aria-label={`Remove ${server}`}
                      className="rounded-md p-1 text-muted hover:bg-surface-2 hover:text-danger"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M18 6 6 18M6 6l12 12" strokeLinecap="round" />
                      </svg>
                    </button>
                  </div>
                </div>

                {!hydrated ? (
                  <p className="mt-2 text-xs text-muted">Loading access…</p>
                ) : members.length === 0 ? (
                  <p className="mt-2 text-xs text-muted">Members unavailable — the whole server is attached.</p>
                ) : (
                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    {members.map(([capability, display]) => {
                      const on = isAllowed(server, capability);
                      return (
                        <button
                          key={capability}
                          type="button"
                          onClick={() => toggleMember(server, capability)}
                          title={on ? `${capability} — allowed (click to deny)` : `${capability} — denied (click to allow)`}
                          aria-pressed={on}
                          className={cn(
                            'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors',
                            on
                              ? 'border-brand bg-brand/10 text-fg'
                              : 'border-border text-faint line-through decoration-faint/60 hover:text-muted',
                          )}
                        >
                          <span className="font-medium no-underline">{display}</span>
                          <span aria-hidden className={cn('text-[11px] leading-none', on ? 'text-brand' : 'text-faint')}>
                            {on ? '✕' : '+'}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ── add controls ─────────────────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="secondary" size="sm" onClick={() => setBrowseOpen(true)}>
          + Browse marketplace
        </Button>
        <button
          type="button"
          onClick={() => setShowRaw((v) => !v)}
          className="ml-auto text-xs text-muted hover:text-fg"
        >
          {showRaw ? 'Hide raw server names' : 'Advanced: raw server names'}
        </button>
      </div>

      {dataQ.error ? (
        <p className="text-xs text-danger">Could not load MCPs / tools. Use the raw field below to enter server names.</p>
      ) : null}

      {showRaw ? (
        <input
          value={servers.join(', ')}
          onChange={(e) =>
            onServersChange(
              e.target.value
                .split(',')
                .map((s) => s.trim())
                .filter(Boolean),
            )
          }
          placeholder="mcp-example-abcd1234, tool-other"
          aria-label="Raw allowed_tools server names"
          className="w-full rounded-md border border-border bg-surface px-3 py-2 font-mono text-xs text-fg placeholder:text-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
        />
      ) : null}

      {/* ── browse modal (the FULL 3-tab × 2-section Marketplace) ─────────────────────────── */}
      <Modal
        open={browseOpen}
        onClose={() => setBrowseOpen(false)}
        title="Browse the marketplace"
        description="Attach an MCP server (all its tools) or a single tool to this agent."
        size="lg"
        footer={
          <Button variant="secondary" onClick={() => setBrowseOpen(false)}>
            Done
          </Button>
        }
      >
        <MarketplaceBrowser
          renderMcpAction={(mcp) => {
            const attached = servers.includes(mcp.server_name);
            return (
              <Button
                size="sm"
                variant={attached ? 'ghost' : 'primary'}
                disabled={attached}
                onClick={() => addMcp(mcp)}
              >
                {attached ? 'Added' : 'Add'}
              </Button>
            );
          }}
          renderToolAction={(tool) => {
            const attached = Boolean(tool.server_name && isAllowed(tool.server_name, tool.capability));
            return (
              <Button
                size="sm"
                variant={attached ? 'ghost' : 'primary'}
                disabled={attached || !tool.server_name}
                onClick={() => addTool(tool)}
              >
                {attached ? 'Added' : 'Add'}
              </Button>
            );
          }}
        />
      </Modal>
    </div>
  );
}
