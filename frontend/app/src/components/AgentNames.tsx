'use client';

import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { useAgentList } from '@/lib/useAgentList';
import { CopyButton } from '@/components/ui/CopyButton';

interface AgentNamesCtx {
  /** Resolve an agent_id to its human name, or null when unknown/unloaded. */
  nameOf: (id?: string | null) => string | null;
  loading: boolean;
}

const Ctx = createContext<AgentNamesCtx>({ nameOf: () => null, loading: false });

/**
 * Session-wide agent_id → name map, fetched ONCE (mounted in AppShell, which is the persistent
 * app layout). Lets any table/detail render an agent by NAME instead of exposing its UUID.
 */
export function AgentNamesProvider({ children }: { children: ReactNode }) {
  const { agents, loading } = useAgentList(200);
  const value = useMemo<AgentNamesCtx>(() => {
    const m = new Map<string, string>();
    for (const a of agents) if (a?.agent_id) m.set(a.agent_id, a.name);
    return { nameOf: (id) => (id ? (m.get(id) ?? null) : null), loading };
  }, [agents, loading]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAgentNames(): AgentNamesCtx {
  return useContext(Ctx);
}

/**
 * Drop-in agent identity: renders the agent's NAME. Never prints the UUID — when the name
 * can't be resolved it falls back to a copy-to-clipboard affordance so the id stays usable
 * for support without being shown.
 */
export function AgentName({ agentId, className }: { agentId?: string | null; className?: string }) {
  const { nameOf, loading } = useAgentNames();
  if (!agentId) return <span className="text-muted">—</span>;
  const name = nameOf(agentId);
  if (name) return <span className={className}>{name}</span>;
  if (loading) return <span className="text-muted">…</span>;
  return <CopyButton value={agentId} label="Copy Agent ID" />;
}
