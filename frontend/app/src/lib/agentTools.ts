/**
 * Shared attach-semantics for the agent tool picker (4C) and the Marketplace "Add to agent" flow
 * (4A). Pure, side-effect-free helpers so the deferred picker (Create Agent), the live picker
 * (Agent Builder), and the standalone Marketplace page all seed per-capability access IDENTICALLY —
 * keeping the two tool stores (`allowed_tools` + `agent_tool_access`) consistent no matter where the
 * attach originates.
 */

import type { AccessMode } from './types';

/** A member tool with its DEFAULT access mode — the shape both the picker and the browser produce. */
export interface SeedMember {
  capability: string;
  access_mode?: AccessMode | null;
}

/**
 * Seed the per-capability access map for adding a WHOLE MCP (spec A1). A member whose default
 * access is `automated` (or unknown — e.g. a platform/public server that exposes no per-member
 * default) comes in ALLOWED; a restricted member (`ask`/`none`) comes in GREYED with an explicit
 * `none` grant, so nothing silently falls back to the registry's permissive `default_access_mode`.
 */
export function seedMcpMemberAccess(members: SeedMember[]): Record<string, AccessMode> {
  const out: Record<string, AccessMode> = {};
  for (const m of members) {
    out[m.capability] = m.access_mode == null || m.access_mode === 'automated' ? 'automated' : 'none';
  }
  return out;
}

/**
 * Seed the per-capability access map for adding a SINGLE tool: the target capability is ALLOWED
 * (`automated`) and every sibling of its containing MCP is GREYED (explicit `none`) — the tool the
 * user picked is the only one they meant to grant.
 */
export function seedToolMemberAccess(members: SeedMember[], capability: string): Record<string, AccessMode> {
  const out: Record<string, AccessMode> = {};
  for (const m of members) out[m.capability] = m.capability === capability ? 'automated' : 'none';
  // Guard: the target must be present even if it wasn't in the member list we were handed.
  out[capability] = 'automated';
  return out;
}
