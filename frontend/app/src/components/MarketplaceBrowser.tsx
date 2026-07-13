'use client';

import { useMemo, useState, type ReactNode } from 'react';
import {
  MarketplaceCard,
  MarketplaceGrid,
  McpMembershipTags,
  OwnerBadge,
  VisibilityBadge,
  capabilityNames,
  cleanMcpName,
  descriptionOf,
  displayMcpTags,
  isAutoSingletonName,
  type McpTag,
} from '@/components/Marketplace';
import { Button, Card, CardBody, CardHeader, ErrorBanner, Loading } from '@/components/ui';
import { listBridgeTools, listMcps, listTools } from '@/lib/services';
import type { AccessMode, BridgeTool, Mcp, ToolView, ToolVisibility } from '@/lib/types';
import { useAsync } from '@/lib/useAsync';
import { cn } from '@/lib/utils';

/**
 * MarketplaceBrowser — the shared 3-tab (Public / Private / Protected) × 2-section (MCP servers /
 * Tools) marketplace layout, extracted from the standalone Marketplace page so BOTH it and the
 * Agent tool-picker popup render the exact same catalogue (spec A4). Each card exposes an optional
 * action slot via {@link MarketplaceBrowserProps.renderMcpAction} / `renderToolAction` — the
 * Marketplace page drops an "Add to agent…" control there, the picker drops an "Add" control.
 *
 * Data model (one load, tabs switch client-side):
 *  - tenant MCPs come from the flow-bridge `listMcps()` (carry per-member `access_mode`), sectioned
 *    into Private/Protected by their own visibility;
 *  - Public MCP servers come from the registry `listTools({ visibility: 'public' })` (platform rows);
 *  - tenant atomic tools come from `listBridgeTools()`, sectioned by visibility; Public tools are the
 *    member capabilities of the Public servers.
 *
 * Standalone tagging (spec B3) is identical on every tab: a tool that is the sole member of an
 * auto-singleton MCP reads as "standalone"; a member of a real multi-tool MCP keeps its "In MCP" tag.
 */

// ── normalized items handed to the action render props ──────────────────────────────────────
/** A member tool of an MCP, with its default access mode (for the picker's allowed/greyed seeding). */
export interface BrowserMember {
  capability: string;
  display_name: string;
  access_mode?: AccessMode | null;
}

/** A normalized MCP server card (tenant collection or public platform server). */
export interface BrowserMcp {
  server_name: string;
  display_name: string;
  description?: string;
  visibility: ToolVisibility;
  isPlatform: boolean;
  members: BrowserMember[];
}

/** A normalized tool card + the context an attach needs (containing server + its members). */
export interface BrowserTool {
  capability: string; // the member/snake name
  display_name: string;
  description?: string;
  visibility: ToolVisibility;
  /** The containing MCP server (first membership) — the server an attach adds to `allowed_tools`. */
  server_name: string | null;
  /** The containing MCP's members — so an attach can grey the siblings the user didn't pick. */
  members: BrowserMember[];
  /** Display memberships (auto-singletons dropped) → drives the standalone-vs-"In MCP" tag. */
  memberships: McpTag[];
}

interface BrowserData {
  mcps: BrowserMcp[];
  tools: BrowserTool[];
  /** At least one source failed — degrade the affected section but keep the rest. */
  errored: boolean;
  /** Every source failed — nothing to show; surface a hard error. */
  allFailed: boolean;
}

// ── normalizers ─────────────────────────────────────────────────────────────────────────────
function tenantMcp(m: Mcp): BrowserMcp {
  return {
    server_name: m.server_name,
    display_name: m.display_name || cleanMcpName(m.server_name),
    description: m.description || undefined,
    visibility: (m.visibility as ToolVisibility) ?? 'private',
    isPlatform: false,
    members: (m.tools ?? []).map((t) => ({
      capability: t.snake_name,
      display_name: t.display_name || t.snake_name,
      access_mode: t.access_mode ?? null,
    })),
  };
}

function publicMcp(s: ToolView): BrowserMcp {
  return {
    server_name: s.name,
    display_name: cleanMcpName(s.name),
    description: descriptionOf(s),
    visibility: 'public',
    isPlatform: s.is_platform ?? true,
    // Public/platform servers expose capability NAMES but no per-member default access mode.
    members: capabilityNames(s).map((n) => ({ capability: n, display_name: n, access_mode: null })),
  };
}

function tenantTool(t: BridgeTool, byServer: Map<string, BrowserMcp>): BrowserTool {
  const server_name = t.mcps?.[0]?.server_name ?? null;
  const container = server_name ? byServer.get(server_name) : undefined;
  return {
    capability: t.snake_name,
    display_name: t.display_name || t.snake_name,
    description: t.description || undefined,
    visibility: (t.visibility as ToolVisibility) ?? 'private',
    server_name,
    members: container?.members ?? [{ capability: t.snake_name, display_name: t.display_name || t.snake_name, access_mode: t.access_mode }],
    memberships: displayMcpTags(t.mcps ?? []),
  };
}

/** One public tool card per member capability of a public server; standalone iff its server is a singleton. */
function publicTool(server: BrowserMcp, member: BrowserMember): BrowserTool {
  const singleton = server.members.length <= 1 || isAutoSingletonName(server.server_name);
  return {
    capability: member.capability,
    display_name: member.display_name,
    visibility: 'public',
    server_name: server.server_name,
    members: server.members,
    memberships: singleton ? [] : [{ server_name: server.server_name }],
  };
}

async function loadBrowserData(signal: AbortSignal): Promise<BrowserData> {
  const [mcpsR, publicR, toolsR] = await Promise.allSettled([
    listMcps(signal),
    listTools({ visibility: 'public' }, signal),
    listBridgeTools(signal),
  ]);

  const mcps: BrowserMcp[] = [];
  const byServer = new Map<string, BrowserMcp>();
  const push = (m: BrowserMcp) => {
    if (!m.server_name || byServer.has(m.server_name)) return;
    byServer.set(m.server_name, m);
    mcps.push(m);
  };

  if (mcpsR.status === 'fulfilled') for (const m of mcpsR.value) push(tenantMcp(m));
  if (publicR.status === 'fulfilled') for (const s of publicR.value) if (s.name) push(publicMcp(s));

  const tools: BrowserTool[] = [];
  if (toolsR.status === 'fulfilled') for (const t of toolsR.value) tools.push(tenantTool(t, byServer));
  if (publicR.status === 'fulfilled') {
    for (const s of publicR.value) {
      const server = s.name ? byServer.get(s.name) : undefined;
      if (!server || server.visibility !== 'public') continue;
      for (const member of server.members) tools.push(publicTool(server, member));
    }
  }

  const results = [mcpsR, publicR, toolsR];
  return {
    mcps,
    tools,
    errored: results.some((r) => r.status === 'rejected'),
    allFailed: results.every((r) => r.status === 'rejected'),
  };
}

// ── tabs ──────────────────────────────────────────────────────────────────────────────────
const TABS: Array<{ key: ToolVisibility; label: string; hint: string }> = [
  { key: 'public', label: 'Public', hint: 'Platform MCP servers and their tools — available to every tenant.' },
  { key: 'private', label: 'Private', hint: 'MCP servers and tools visible only to your tenant.' },
  {
    key: 'protected',
    label: 'Protected',
    hint: 'Your tenant plus explicit grants. Grant management is coming soon; today these behave like private.',
  },
];

export interface MarketplaceBrowserProps {
  /** Which tab is selected first. Default `public`. */
  initialTab?: ToolVisibility;
  /** Per-card action for an MCP server (e.g. an "Add" / "Add to agent…" control). */
  renderMcpAction?: (mcp: BrowserMcp) => ReactNode;
  /** Per-card action for a tool. */
  renderToolAction?: (tool: BrowserTool) => ReactNode;
  className?: string;
}

export function MarketplaceBrowser({ initialTab = 'public', renderMcpAction, renderToolAction, className }: MarketplaceBrowserProps) {
  const [tab, setTab] = useState<ToolVisibility>(initialTab);
  const active = TABS.find((t) => t.key === tab) ?? TABS[0];
  const { data, loading, error, reload } = useAsync((signal) => loadBrowserData(signal), []);

  const mcps = useMemo(() => (data?.mcps ?? []).filter((m) => m.visibility === tab), [data, tab]);
  const tools = useMemo(() => (data?.tools ?? []).filter((t) => t.visibility === tab), [data, tab]);

  const hardError = error || data?.allFailed;

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {/* ── toolbar: visibility tabs + refresh ─────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="inline-flex rounded-md border border-border bg-surface p-0.5" role="tablist" aria-label="Visibility">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={tab === t.key}
              onClick={() => setTab(t.key)}
              className={cn(
                'rounded px-3 py-1.5 text-sm font-medium transition-colors',
                tab === t.key ? 'bg-surface-2 text-fg-strong' : 'text-muted hover:text-fg',
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        <Button variant="secondary" size="sm" onClick={reload}>
          Refresh
        </Button>
      </div>
      <p className="text-xs text-muted">{active.hint}</p>

      {hardError ? (
        <ErrorBanner
          error={error ?? new Error('The marketplace is unavailable right now.')}
          title="Could not load the marketplace"
        />
      ) : loading ? (
        <Loading label="Loading marketplace…" />
      ) : (
        <>
          {data?.errored ? (
            <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-warning">
              Some of the marketplace could not be loaded — showing what is available.
            </p>
          ) : null}

          {/* ── MCP servers ─────────────────────────────────────────────────────────────── */}
          <Card>
            <CardHeader
              title="MCP Servers"
              description="Aggregating MCP servers your agents can attach. Attaching one grants all its member tools."
              actions={<span className="font-mono text-xs tabular-nums text-muted">{mcps.length}</span>}
            />
            <CardBody>
              {mcps.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted">No {active.label.toLowerCase()} MCP servers yet.</p>
              ) : (
                <MarketplaceGrid>
                  {mcps.map((m) => (
                    <MarketplaceCard
                      key={m.server_name}
                      title={m.display_name}
                      subtitle={m.server_name}
                      description={m.description}
                      badges={
                        <>
                          <VisibilityBadge visibility={m.visibility} />
                          <OwnerBadge isPlatform={m.isPlatform} />
                        </>
                      }
                      meta={
                        <span className="font-mono text-xs tabular-nums text-muted">
                          {m.members.length} {m.members.length === 1 ? 'tool' : 'tools'}
                        </span>
                      }
                      action={renderMcpAction?.(m)}
                    />
                  ))}
                </MarketplaceGrid>
              )}
            </CardBody>
          </Card>

          {/* ── Tools ───────────────────────────────────────────────────────────────────── */}
          <Card>
            <CardHeader
              title="Tools"
              description={
                tab === 'public'
                  ? 'Capabilities exposed by the Public (platform) MCP servers above.'
                  : 'Your atomic tools, tagged with the MCP(s) they belong to.'
              }
              actions={<span className="font-mono text-xs tabular-nums text-muted">{tools.length}</span>}
            />
            <CardBody>
              {tools.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted">No {active.label.toLowerCase()} tools yet.</p>
              ) : (
                <MarketplaceGrid>
                  {tools.map((t) => (
                    <MarketplaceCard
                      key={`${t.server_name ?? 'none'}:${t.capability}`}
                      title={t.display_name}
                      subtitle={t.capability}
                      description={t.description}
                      badges={<VisibilityBadge visibility={t.visibility} />}
                      meta={
                        <div className="flex flex-col gap-1">
                          <span className="text-[11px] font-semibold uppercase tracking-wider text-faint">
                            {t.memberships.length === 0 ? 'Membership' : t.memberships.length === 1 ? 'In MCP' : 'In MCPs'}
                          </span>
                          <McpMembershipTags memberships={t.memberships} />
                        </div>
                      }
                      action={renderToolAction?.(t)}
                    />
                  ))}
                </MarketplaceGrid>
              )}
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
}
