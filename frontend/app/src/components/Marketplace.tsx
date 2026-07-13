import type { ReactNode } from 'react';
import { Badge } from '@/components/ui';
import { cn } from '@/lib/utils';
import type { ToolMcpMembership, ToolView, ToolVisibility } from '@/lib/types';

/**
 * Shared, presentation-only building blocks for the Marketplace (Phase 4A) that the Tool Builder
 * (4B) and the Agent picker (4C) reuse: a {@link VisibilityBadge}, an {@link OwnerBadge}, the
 * {@link McpMembershipTags} that tag a tool with the MCP(s) it belongs to, a {@link MarketplaceGrid}
 * responsive layout, and a generic {@link MarketplaceCard} tile.
 *
 * SEAM (Phase 4C): every tile exposes an optional `action` slot — 4C drops an "Add to agent"
 * control there. It is intentionally left unset in 4A so the Marketplace stays browse-only.
 */

// ── VisibilityBadge ───────────────────────────────────────────────────────────────────
const VISIBILITY_TONE: Record<ToolVisibility, 'info' | 'warning' | 'neutral'> = {
  public: 'info', // platform-shared, discoverable by every tenant
  protected: 'warning', // owner + explicit grants (grant logic is future)
  private: 'neutral', // owner tenant only
};

const VISIBILITY_LABEL: Record<ToolVisibility, string> = {
  public: 'Public',
  protected: 'Protected',
  private: 'Private',
};

function asVisibility(v: string | null | undefined): ToolVisibility | null {
  return v === 'public' || v === 'protected' || v === 'private' ? v : null;
}

/** A chip for a tool/MCP tenant-visibility label (`public`|`protected`|`private`). */
export function VisibilityBadge({ visibility }: { visibility?: string | null }) {
  const v = asVisibility(visibility);
  if (!v) return <span className="text-faint">—</span>;
  return <Badge tone={VISIBILITY_TONE[v]}>{VISIBILITY_LABEL[v]}</Badge>;
}

// ── OwnerBadge ────────────────────────────────────────────────────────────────────────
/** Platform vs tenant ownership chip. Prefers `isPlatform`, falls back to the `owner` string. */
export function OwnerBadge({ isPlatform, owner }: { isPlatform?: boolean; owner?: string | null }) {
  const kind: 'platform' | 'tenant' | null =
    typeof isPlatform === 'boolean'
      ? isPlatform
        ? 'platform'
        : 'tenant'
      : owner === 'platform' || owner === 'tenant'
        ? owner
        : null;
  if (kind === 'platform') return <Badge tone="info">Platform</Badge>;
  if (kind === 'tenant') return <Badge>Tenant</Badge>;
  return <span className="text-faint">—</span>;
}

// ── McpMembershipTags ───────────────────────────────────────────────────────────────────
/** Strip the tenant/platform noise from a server/slug name for a compact tag label. */
export function cleanMcpName(name: string): string {
  return name
    .replace(/^mcp-/, '')
    .replace(/^tool-/, '')
    .replace(/-[0-9a-f]{8}$/, '');
}

/**
 * True when a server/slug name is an auto-created SINGLETON MCP that wraps exactly one tool
 * (`tool-<slug>` for a legacy/tenant singleton). Such a wrapper is an implementation detail — a
 * tool reachable only through it should read as "standalone", not as a real MCP membership.
 */
export function isAutoSingletonName(name: string | null | undefined): boolean {
  return typeof name === 'string' && /^tool-/.test(name);
}

/** Membership shape is intentionally loose so 4C/derived (public) cards can pass a partial. */
export type McpTag = Pick<ToolMcpMembership, 'server_name'> & { slug?: string; mcp_id?: string };

/**
 * The MCP memberships to DISPLAY for a tool card — auto-singleton wrappers are dropped so a tool
 * whose only home is its singleton reads as "standalone" ({@link McpMembershipTags} renders the
 * standalone chip for an empty list), while real multi-tool memberships keep their "In MCP" tag.
 * Shared by the Public / Private / Protected tabs so standalone derivation is identical everywhere.
 */
export function displayMcpTags(memberships: McpTag[]): McpTag[] {
  return memberships.filter((m) => !isAutoSingletonName(m.server_name || m.slug));
}

/**
 * Render the MCP(s) a tool belongs to as small chips. When there are none, show a single
 * "standalone" chip (every tool is still reachable via its auto-singleton MCP).
 */
export function McpMembershipTags({
  memberships,
  standaloneLabel = 'standalone',
  className,
}: {
  memberships: McpTag[];
  standaloneLabel?: string;
  className?: string;
}) {
  if (memberships.length === 0) {
    return (
      <span className={cn('inline-flex', className)}>
        <Badge tone="neutral">{standaloneLabel}</Badge>
      </span>
    );
  }
  return (
    <span className={cn('inline-flex flex-wrap gap-1', className)}>
      {memberships.map((m, i) => {
        const raw = m.server_name || m.slug || '—';
        return (
          <Badge key={m.mcp_id ?? m.slug ?? `${raw}-${i}`} tone="neutral" className="font-mono">
            <span title={raw}>{cleanMcpName(raw)}</span>
          </Badge>
        );
      })}
    </span>
  );
}

// ── registry ToolView readers (the ToolView is permissive + may be partial) ───────────────
function asStr(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

function manifestOf(server: ToolView): Record<string, unknown> | null {
  return server.manifest && typeof server.manifest === 'object' ? (server.manifest as Record<string, unknown>) : null;
}

/** A registry MCP-server's description — top-level, else the manifest's. */
export function descriptionOf(server: ToolView): string | undefined {
  return asStr(server.description) ?? asStr(manifestOf(server)?.description);
}

/** The member-tool NAMES a registry server exposes — from `capabilities[]`, else manifest `tools[]`. */
export function capabilityNames(server: ToolView): string[] {
  const caps: unknown[] = Array.isArray(server.capabilities) ? server.capabilities : [];
  const names = caps
    .map((c) => {
      if (typeof c === 'string') return c;
      if (c && typeof c === 'object') {
        const o = c as Record<string, unknown>;
        return asStr(o.name) ?? asStr(o.capability);
      }
      return undefined;
    })
    .filter((n): n is string => Boolean(n));
  if (names.length) return names;
  const manifest = manifestOf(server);
  const tools = manifest && Array.isArray(manifest.tools) ? manifest.tools : [];
  return tools
    .map((t) => (t && typeof t === 'object' ? asStr((t as Record<string, unknown>).name) : undefined))
    .filter((n): n is string => Boolean(n));
}

// ── MarketplaceGrid ─────────────────────────────────────────────────────────────────────
/** Responsive card grid — one column on phones, up to three on wide screens. */
export function MarketplaceGrid({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3', className)}>{children}</div>
  );
}

// ── MarketplaceCard ─────────────────────────────────────────────────────────────────────
export interface MarketplaceCardProps {
  title: ReactNode;
  /** Mono sub-line under the title (e.g. an MCP server name or a tool's snake_name). */
  subtitle?: ReactNode;
  description?: ReactNode;
  /** Top-right chips (visibility / owner / status). */
  badges?: ReactNode;
  /** Footer meta row (membership tags, capability counts). */
  meta?: ReactNode;
  /**
   * SEAM (Phase 4C): "Add to agent" action slot. Rendered in the card footer when set; left unset
   * in 4A so the Marketplace is read/browse-only.
   */
  action?: ReactNode;
  className?: string;
}

/** A flat browse tile matching the console's Card styling. Presentation only. */
export function MarketplaceCard({
  title,
  subtitle,
  description,
  badges,
  meta,
  action,
  className,
}: MarketplaceCardProps) {
  return (
    <div className={cn('flex flex-col rounded-md border border-border bg-surface p-3.5', className)}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-fg-strong">{title}</p>
          {subtitle ? <p className="mt-0.5 truncate font-mono text-xs text-muted">{subtitle}</p> : null}
        </div>
        {badges ? <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">{badges}</div> : null}
      </div>

      {description ? <p className="mt-2 line-clamp-3 text-xs text-muted">{description}</p> : null}

      {meta || action ? (
        <div className="mt-3 flex items-end justify-between gap-2 border-t border-border pt-2.5">
          <div className="min-w-0">{meta}</div>
          {action ? <div className="shrink-0">{action}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
