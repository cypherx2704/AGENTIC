'use client';

import type { ReactNode } from 'react';
import {
  Badge,
  Button,
  Card,
  CardBody,
  ErrorBanner,
  Loading,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAgentList } from '@/lib/useAgentList';
import type { Agent } from '@/lib/types';
import { formatTime, shortId } from '@/lib/utils';

export interface AgentListProps {
  /** Chosen agent — fired on row click (and on the action button, if `actionLabel` is set). */
  onSelect: (agent: Agent) => void;
  /**
   * When provided, render a trailing action column with this label in addition to making the whole row
   * clickable (e.g. "Manage keys"). The button stops propagation so it and the row don't double-fire.
   */
  actionLabel?: string;
  /** Extra UI rendered in BOTH the error and empty states (e.g. an open-by-id fallback). */
  fallback?: ReactNode;
  /** Empty-state copy. */
  emptyLabel?: ReactNode;
}

/**
 * Reusable, cursor-paginated agent table backed by {@link useAgentList}. Owns the loading / error /
 * empty / "Load more" presentation so every screen that lists agents looks and behaves identically;
 * callers supply only the per-row action. The single source of truth for the data is the hook.
 */
export function AgentList({ onSelect, actionLabel, fallback, emptyLabel }: AgentListProps) {
  const toast = useToast();
  const { agents, loading, loadingMore, error, hasMore, reload, loadMore } = useAgentList();

  const columns: Array<Column<Agent>> = [
    { key: 'name', header: 'Name', render: (a) => <span className="font-medium text-fg">{a.name}</span> },
    {
      key: 'agent_id',
      header: 'Agent ID',
      render: (a) => <span className="font-mono text-xs text-muted">{shortId(a.agent_id, 12)}</span>,
    },
    { key: 'status', header: 'Status', render: (a) => <StatusBadge status={a.status} /> },
    {
      key: 'scopes',
      header: 'Allowed scopes',
      render: (a) => (
        <div className="flex flex-wrap gap-1">
          {(a.allowed_scopes ?? []).slice(0, 3).map((s) => (
            <Badge key={s}>{s}</Badge>
          ))}
          {(a.allowed_scopes?.length ?? 0) > 3 && <Badge>+{(a.allowed_scopes?.length ?? 0) - 3}</Badge>}
        </div>
      ),
    },
    { key: 'created', header: 'Created', render: (a) => <span className="text-xs text-muted">{formatTime(a.created_at)}</span> },
  ];

  if (actionLabel) {
    columns.push({
      key: 'action',
      header: '',
      className: 'text-right',
      render: (a) => (
        <Button
          size="sm"
          variant="secondary"
          onClick={(e) => {
            e.stopPropagation();
            onSelect(a);
          }}
        >
          {actionLabel}
        </Button>
      ),
    });
  }

  async function onLoadMore() {
    try {
      await loadMore();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not load more agents.');
    }
  }

  if (error) {
    return (
      <Card>
        <CardBody>
          <ErrorBanner error={error} title="Could not load agents" />
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <Button size="sm" variant="secondary" onClick={() => void reload()}>
              Retry
            </Button>
            {fallback}
          </div>
        </CardBody>
      </Card>
    );
  }

  if (loading) return <Loading label="Loading agents…" />;

  return (
    <Card>
      <Table
        columns={columns}
        rows={agents}
        rowKey={(a) => a.agent_id}
        onRowClick={(a) => onSelect(a)}
        empty={
          <div className="space-y-3">
            <p>{emptyLabel ?? 'No agents yet.'}</p>
            {fallback}
          </div>
        }
      />
      {hasMore && (
        <div className="flex items-center justify-center gap-3 border-t border-border p-3">
          <span className="text-xs text-muted">{agents.length} loaded</span>
          <Button size="sm" variant="secondary" loading={loadingMore} onClick={() => void onLoadMore()}>
            Load more
          </Button>
        </div>
      )}
    </Card>
  );
}
