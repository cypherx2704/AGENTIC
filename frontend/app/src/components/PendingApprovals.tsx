'use client';

import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, CardBody, CardHeader, useToast } from '@/components/ui';
import { denyHilApproval, grantHilApproval, listHilApprovals, type HilApproval } from '@/lib/services';

/**
 * Inline HIL approvals for a running orchestration (F4). When the orchestrator's HIL mode pauses a
 * sub-agent node, the run's execution tree shows it `awaiting_approval`; this surfaces the matching
 * pending approval(s) for THIS run (filtered by the approval context's workflow_id) with Grant/Deny,
 * so the human resolves it without leaving the run — the driver's poll then resumes the node.
 */
export function PendingApprovals({
  workflowId,
  active,
  onResolved,
}: {
  workflowId: string;
  active: boolean;
  onResolved?: () => void;
}) {
  const toast = useToast();
  const [items, setItems] = useState<HilApproval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await listHilApprovals({});
      setItems(
        (resp.items ?? []).filter(
          (a) => a.status === 'pending' && (a.context?.workflow_id as string | undefined) === workflowId,
        ),
      );
    } catch {
      /* best-effort — the tree still reflects awaiting_approval */
    }
  }, [workflowId]);

  useEffect(() => {
    if (!active) {
      setItems([]);
      return;
    }
    void load();
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [active, load]);

  async function resolve(a: HilApproval, decision: 'grant' | 'deny') {
    setBusy(a.request_id);
    try {
      if (decision === 'grant') await grantHilApproval(a.request_id);
      else await denyHilApproval(a.request_id);
      toast.success(`Sub-agent ${decision === 'grant' ? 'approved' : 'denied'}.`);
      setItems((prev) => prev.filter((x) => x.request_id !== a.request_id));
      onResolved?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setBusy(null);
    }
  }

  if (!active || items.length === 0) return null;

  return (
    <Card className="border-warning/40">
      <CardHeader
        title="Approval Needed"
        description="The orchestrator is waiting on your decision before delegating to a sub-agent."
      />
      <CardBody className="flex flex-col gap-2">
        {items.map((a) => (
          <div
            key={a.request_id}
            className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-2 px-3 py-2"
          >
            <Badge tone="warning">{a.operation_type ?? 'approval'}</Badge>
            <span className="text-sm text-fg">
              {String(a.context?.node_id ?? 'node')}
              {a.context?.preset ? <span className="text-muted"> · {String(a.context.preset)}</span> : null}
            </span>
            <div className="ml-auto flex gap-2">
              <Button size="sm" loading={busy === a.request_id} onClick={() => void resolve(a, 'grant')}>
                Grant
              </Button>
              <Button size="sm" variant="danger" loading={busy === a.request_id} onClick={() => void resolve(a, 'deny')}>
                Deny
              </Button>
            </div>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}
