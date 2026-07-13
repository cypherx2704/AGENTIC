'use client';

import Link from 'next/link';
import { use, useEffect, useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  ErrorBanner,
  Loading,
  Modal,
  Select,
  StatusBadge,
  useToast,
} from '@/components/ui';
import { ScopeSelector } from '@/components/ScopeSelector';
import { useSession } from '@/components/SessionProvider';
import { BffError } from '@/lib/bff-client';
import { useAsync } from '@/lib/useAsync';
import {
  deactivateAgent,
  getAgent,
  getRuntime,
  listKnowledgeBases,
  listModels,
  listPolicies,
  revokeAllTokens,
  updateAgent,
} from '@/lib/services';
import type { Agent, AgentRuntime, KnowledgeBase, Policy } from '@/lib/types';
import { formatTime } from '@/lib/utils';
import { AgentBuilder } from './AgentBuilder';

export default function AgentDetailPage({ params }: { params: Promise<{ agentId: string }> }) {
  const { agentId } = use(params);

  const agentQ = useAsync((signal) => getAgent(agentId, signal), [agentId]);
  const modelsQ = useAsync((signal) => listModels(signal), []);
  // A 404 here is expected (no runtime registered yet) — treat it as "null runtime".
  const runtimeQ = useAsync<AgentRuntime | null>(
    (signal) =>
      getRuntime(agentId, signal).catch((err) => {
        if (err instanceof BffError && err.status === 404) return null;
        throw err;
      }),
    [agentId],
  );
  // Optional selectors for the Builder — degrade to empty (free-text fallback) if unavailable.
  const policiesQ = useAsync<Policy[]>(
    (signal) => listPolicies(signal).then((r) => r.policies ?? []).catch(() => []),
    [],
  );
  const kbsQ = useAsync<KnowledgeBase[]>(
    (signal) => listKnowledgeBases(signal).catch(() => []),
    [],
  );

  const agent = agentQ.data;
  const models = modelsQ.data?.data ?? [];

  return (
    <Page>
      <PageHeader
        title={agent ? agent.name : 'Agent'}
        description={<CopyButton value={agentId} label="Copy Agent ID" />}
        actions={
          <>
            {agent && <StatusBadge status={agent.status} />}
            <Link href="/agents" className="text-[13px] font-medium text-brand hover:underline">
              ← All Agents
            </Link>
          </>
        }
      />

      <PageBody>
        {agentQ.error ? (
          <ErrorBanner error={agentQ.error} title="Could not load this agent" className="mb-4" />
        ) : null}

        {agentQ.loading ? (
          <Loading label="Loading agent…" />
        ) : (
          <div className="flex flex-col gap-3">
            {agent && (
              <Card>
                <CardHeader
                  title="Identity"
                  description="From the Auth service — the source of truth for an agent's tenant."
                  actions={<AgentActions agent={agent} onChanged={(a) => agentQ.setData(a)} />}
                />
                <CardBody>
                  <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                    <Field label="Status" value={<StatusBadge status={agent.status} />} />
                    <Field label="Version" value={agent.version || '—'} />
                    <Field label="Created" value={formatTime(agent.created_at)} />
                    <Field label="Updated" value={formatTime(agent.updated_at)} />
                    <div className="col-span-2 sm:col-span-4">
                      <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">Allowed Scopes</p>
                      <div className="flex flex-wrap gap-1">
                        {(agent.allowed_scopes ?? []).map((s) => (
                          <Badge key={s} tone="info">
                            {s}
                          </Badge>
                        ))}
                        {(agent.allowed_scopes?.length ?? 0) === 0 && <span className="text-sm text-muted">none</span>}
                      </div>
                    </div>
                  </dl>
                </CardBody>
              </Card>
            )}

            {runtimeQ.loading ? (
              <Loading label="Loading runtime config…" />
            ) : runtimeQ.error ? (
              <ErrorBanner error={runtimeQ.error} title="Could not load runtime config" />
            ) : (
              <AgentBuilder
                agentId={agentId}
                fallbackName={agent?.name ?? agentId}
                initialRuntime={runtimeQ.data}
                models={models}
                policies={policiesQ.data ?? []}
                kbs={kbsQ.data ?? []}
                onSaved={(rt) => runtimeQ.setData(rt)}
              />
            )}
          </div>
        )}
      </PageBody>
    </Page>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</p>
      <p className="mt-1 text-sm text-fg">{value}</p>
    </div>
  );
}

/**
 * Valid `reason` values for the Auth revocation endpoints. The backend validates the reason
 * against this exact enum (`RevocationReason`) and 400s on anything else — so this is a Select,
 * never a free-text field.
 */
const REVOCATION_REASONS = [
  { value: 'admin_action', label: 'Admin Action' },
  { value: 'compromised', label: 'Compromised' },
  { value: 'rotated', label: 'Rotated' },
  { value: 'policy_violation', label: 'Policy Violation' },
  { value: 'deactivated', label: 'Deactivated' },
] as const;

/** Statuses in which the agent is already deactivated/suspended — hide the Deactivate action. */
const INACTIVE_STATUSES = new Set(['inactive', 'suspended', 'deactivated']);

/**
 * Credential-control actions for an agent's Identity card: edit its allowed scopes, revoke all
 * outstanding tokens (force re-auth), and deactivate it (cascade-revokes keys + tokens). Each
 * successful mutation refreshes the page's agent in place via the caller's `onChanged` (the
 * existing `agentQ.setData`), so the Identity card + status badge update without a refetch.
 */
function AgentActions({ agent, onChanged }: { agent: Agent; onChanged: (agent: Agent) => void }) {
  const toast = useToast();
  const { session } = useSession();
  const [editOpen, setEditOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [deactivateOpen, setDeactivateOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState<string>('admin_action');
  const [busy, setBusy] = useState(false);
  const [editError, setEditError] = useState<unknown>(null);

  const canDeactivate = !INACTIVE_STATUSES.has((agent.status ?? '').toLowerCase());

  // Prime the scopes editor from the current agent each time the modal opens.
  useEffect(() => {
    if (editOpen) {
      setSelected(agent.allowed_scopes ?? []);
      setEditError(null);
    }
  }, [editOpen, agent]);

  async function onSaveScopes(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setEditError(null);
    try {
      const updated = await updateAgent(agent.agent_id, { allowed_scopes: selected });
      onChanged(updated);
      toast.success('Allowed scopes updated.');
      setEditOpen(false);
    } catch (err) {
      setEditError(err);
    } finally {
      setBusy(false);
    }
  }

  async function onRevokeAll() {
    setBusy(true);
    try {
      const result = await revokeAllTokens(agent.agent_id, reason);
      const count = typeof result?.revoked_count === 'number' ? result.revoked_count : null;
      toast.success(
        count === null
          ? 'All tokens revoked; the agent must re-authenticate.'
          : `${count} token${count === 1 ? '' : 's'} revoked; the agent must re-authenticate.`,
      );
      setRevokeOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not revoke tokens.');
    } finally {
      setBusy(false);
    }
  }

  async function onDeactivate() {
    setBusy(true);
    try {
      const result = await deactivateAgent(agent.agent_id);
      onChanged(result.agent);
      toast.success(
        `Agent deactivated · ${result.keys_revoked} key${result.keys_revoked === 1 ? '' : 's'} and ` +
          `${result.tokens_revoked} token${result.tokens_revoked === 1 ? '' : 's'} revoked.`,
      );
      setDeactivateOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not deactivate the agent.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button variant="secondary" size="sm" onClick={() => setEditOpen(true)}>
        Edit Scopes
      </Button>
      <Button variant="secondary" size="sm" onClick={() => setRevokeOpen(true)}>
        Revoke Tokens
      </Button>
      {canDeactivate && (
        <Button variant="danger" size="sm" onClick={() => setDeactivateOpen(true)}>
          Deactivate
        </Button>
      )}

      <Modal
        open={editOpen}
        onClose={() => {
          if (!busy) setEditOpen(false);
        }}
        title="Edit Allowed Scopes"
        description="These bound every scope a key for this agent can request."
        footer={
          <>
            <Button variant="secondary" onClick={() => setEditOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button form="edit-scopes-form" type="submit" loading={busy}>
              Save Scopes
            </Button>
          </>
        }
      >
        <form id="edit-scopes-form" onSubmit={onSaveScopes} className="flex flex-col gap-4">
          <ScopeSelector
            available={session?.scopes ?? []}
            value={selected}
            onChange={setSelected}
          />
          {editError ? <ErrorBanner error={editError} /> : null}
          {/* Tool + MCP access is managed by the Agent Builder's tool picker below the Identity card. */}
        </form>
      </Modal>

      <ConfirmDialog
        open={revokeOpen}
        onClose={() => setRevokeOpen(false)}
        onConfirm={onRevokeAll}
        title="Revoke all tokens?"
        description="Every outstanding token for this agent is revoked immediately."
        confirmLabel="Revoke Tokens"
        loading={busy}
      >
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted">
            The agent must re-authenticate with a valid API key to obtain a new token. Its API keys are
            not affected.
          </p>
          <Select label="Reason" value={reason} onChange={(e) => setReason(e.target.value)} disabled={busy}>
            {REVOCATION_REASONS.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </Select>
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={deactivateOpen}
        onClose={() => setDeactivateOpen(false)}
        onConfirm={onDeactivate}
        title="Deactivate this agent?"
        description="This cascades — all API keys and all outstanding tokens are revoked."
        confirmLabel="Deactivate Agent"
        loading={busy}
      >
        <p className="text-sm text-muted">
          The agent is set to <span className="text-fg">inactive</span>. Every API key is revoked and
          every live token is killed, so any integration using this agent stops working immediately.
          You can issue new keys afterwards, but this is not auto-undone.
        </p>
      </ConfirmDialog>
    </>
  );
}
