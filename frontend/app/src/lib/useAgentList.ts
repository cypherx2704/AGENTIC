'use client';

import { useCallback, useEffect, useState } from 'react';
import { listAgents } from './services';
import type { Agent } from './types';

const DEFAULT_PAGE_SIZE = 50;

export interface UseAgentListResult {
  /** Accumulated agents across the pages loaded so far. */
  agents: Agent[];
  /** First-page load (also true during a `reload`). */
  loading: boolean;
  /** A `loadMore` (next-page) fetch is in flight. */
  loadingMore: boolean;
  /** First-page error (aborts are swallowed). `loadMore` rejects instead of setting this. */
  error: unknown;
  /** True while the server reports a further page (next_cursor present). */
  hasMore: boolean;
  /** (Re)load the first page; pass an AbortSignal to cancel on unmount. */
  reload: (signal?: AbortSignal) => Promise<void>;
  /** Append the next page. Resolves immediately when there is nothing more; rejects on transport error. */
  loadMore: () => Promise<void>;
}

/**
 * Stateful, cursor-paginated loader for the tenant's agents (auth `GET /v1/agents`, Contract-9 cursor
 * shape `{ items, next_cursor }`). It is the single source of truth for "list the agents" so every
 * screen (Agents, the API-keys agent picker) shares one implementation of page accumulation, the
 * next-cursor, and the loading/error lifecycle.
 *
 * Conventions match the rest of the SPA: `reload` swallows AbortErrors (unmount / re-fetch) and routes
 * everything else to `error`; `loadMore` does NOT swallow — it rejects so the caller can surface a toast
 * without clobbering the first-page error state. `items` is authoritative; `agents`/`data` are tolerated
 * fallbacks for an alternate gateway shape.
 */
export function useAgentList(pageSize: number = DEFAULT_PAGE_SIZE): UseAgentListResult {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const resp = await listAgents({ limit: pageSize }, signal);
        setAgents(resp.items ?? resp.agents ?? resp.data ?? []);
        setCursor(resp.next_cursor ?? null);
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) setError(err);
      } finally {
        setLoading(false);
      }
    },
    [pageSize],
  );

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const resp = await listAgents({ cursor, limit: pageSize });
      setAgents((prev) => [...prev, ...(resp.items ?? resp.agents ?? resp.data ?? [])]);
      setCursor(resp.next_cursor ?? null);
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, loadingMore, pageSize]);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  return { agents, loading, loadingMore, error, hasMore: cursor != null, reload, loadMore };
}
