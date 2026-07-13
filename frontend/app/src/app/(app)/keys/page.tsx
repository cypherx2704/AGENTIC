'use client';

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { AgentList } from '@/components/AgentList';
import { AgentName } from '@/components/AgentNames';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  Select,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAgentList } from '@/lib/useAgentList';
import { useAsync } from '@/lib/useAsync';
import { createKey, listKeys, revokeKey, rotateAgentKey } from '@/lib/services';
import type { ApiKeyListItem, CreateKeyResponse, RotateKeyResponse } from '@/lib/types';
import { formatTime } from '@/lib/utils';

function KeysInner() {
  const router = useRouter();
  const params = useSearchParams();
  const toast = useToast();
  const agentId = params.get('agent') ?? '';

  const selectAgent = (id: string) => {
    const trimmed = id.trim();
    router.push(trimmed ? `/keys?agent=${encodeURIComponent(trimmed)}` : '/keys');
  };

  // An agent is selected -> manage its keys (deep-linkable via ?agent=).
  if (agentId) {
    return (
      <Page>
        <PageHeader
          title="API Keys"
          description="Issue and revoke this agent's API keys. The raw secret is shown exactly once."
          actions={
            <Link href="/keys" className="text-[13px] font-medium text-brand hover:underline">
              ← All Agents
            </Link>
          }
        />
        <PageBody fill>
          <KeyManager agentId={agentId} toastError={toast.error} />
        </PageBody>
      </Page>
    );
  }

  // No agent selected -> pick one from the tenant's agents (or paste an id as a fallback).
  return (
    <Page>
      <PageHeader title="API Keys" description="Choose an agent to manage its API keys." />
      <PageBody>
        <AgentList
          onSelect={(a) => selectAgent(a.agent_id)}
          actionLabel="Manage Keys"
          emptyLabel="No agents yet — create one on the Agents page first."
          fallback={<ManageById onSubmit={selectAgent} />}
        />
      </PageBody>
    </Page>
  );
}

/** Fallback: pick an agent by NAME to jump to its keys — never surfaces the raw agent UUID. */
function ManageById({ onSubmit }: { onSubmit: (id: string) => void }) {
  const { agents, loading } = useAgentList();
  const [id, setId] = useState('');
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (id.trim()) onSubmit(id.trim());
      }}
      className="flex items-end gap-2"
    >
      <Select
        label="Agent"
        value={id}
        onChange={(e) => setId(e.target.value)}
        className="w-72"
        disabled={loading || agents.length === 0}
      >
        <option value="">{loading ? 'Loading agents…' : 'Select an agent…'}</option>
        {agents.map((a) => (
          <option key={a.agent_id} value={a.agent_id}>
            {a.name}
          </option>
        ))}
      </Select>
      <Button type="submit" size="md" variant="secondary" disabled={!id.trim()}>
        Manage Keys
      </Button>
    </form>
  );
}

function KeyManager({ agentId, toastError }: { agentId: string; toastError: (m: string) => void }) {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listKeys(agentId, signal), [agentId]);
  const [createOpen, setCreateOpen] = useState(false);
  // The reveal modal is reused for BOTH issue and rotate — a rotation carries the extra
  // previous-key grace fields, distinguished structurally inside RawKeyModal.
  const [revealKey, setRevealKey] = useState<CreateKeyResponse | RotateKeyResponse | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<ApiKeyListItem | null>(null);
  const [rotating, setRotating] = useState<string | null>(null);
  const [confirmRotate, setConfirmRotate] = useState<ApiKeyListItem | null>(null);

  const keys = data?.keys ?? [];

  async function onRevoke(key: ApiKeyListItem) {
    setRevoking(key.key_id);
    try {
      await revokeKey(agentId, key.key_id);
      toast.success('Key revoked.');
      setConfirmRevoke(null);
      reload();
    } catch (err) {
      toastError(err instanceof Error ? err.message : 'Revoke failed.');
    } finally {
      setRevoking(null);
    }
  }

  async function onRotate(key: ApiKeyListItem) {
    setRotating(key.key_id);
    try {
      const resp = await rotateAgentKey(agentId, key.key_id);
      setConfirmRotate(null);
      // Surface the new secret ONCE via the same reveal modal the create flow uses.
      setRevealKey(resp);
      reload();
    } catch (err) {
      toastError(err instanceof Error ? err.message : 'Rotation failed.');
    } finally {
      setRotating(null);
    }
  }

  const columns: Array<Column<ApiKeyListItem>> = [
    { key: 'prefix', header: 'Prefix', render: (k) => <span className="font-mono text-xs text-fg">{k.key_prefix}…</span> },
    { key: 'name', header: 'Name', render: (k) => k.name ?? <span className="text-muted">—</span> },
    {
      key: 'scopes',
      header: 'Scopes',
      render: (k) => (
        <div className="flex flex-wrap gap-1">
          {k.scopes.map((s) => (
            <Badge key={s}>{s}</Badge>
          ))}
        </div>
      ),
    },
    { key: 'status', header: 'Status', render: (k) => <StatusBadge status={k.status} /> },
    { key: 'created', header: 'Created', render: (k) => <span className="text-xs text-muted">{formatTime(k.created_at)}</span> },
    { key: 'last_used', header: 'Last Used', render: (k) => <span className="text-xs text-muted">{formatTime(k.last_used_at)}</span> },
    {
      key: 'actions',
      header: '',
      render: (k) =>
        k.status === 'active' ? (
          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              loading={rotating === k.key_id}
              disabled={revoking === k.key_id}
              onClick={() => setConfirmRotate(k)}
            >
              Rotate
            </Button>
            <Button
              variant="danger"
              size="sm"
              loading={revoking === k.key_id}
              disabled={rotating === k.key_id}
              onClick={() => setConfirmRevoke(k)}
            >
              Revoke
            </Button>
          </div>
        ) : null,
    },
  ];

  return (
    <>
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <CardHeader
        title="Keys"
        description={<AgentName agentId={agentId} />}
        actions={
          <Button size="md" onClick={() => setCreateOpen(true)}>
            New Key
          </Button>
        }
      />
      <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
        {error ? (
          <div className="p-4">
            <ErrorBanner error={error} title="Could not load keys" />
          </div>
        ) : loading ? (
          <Loading label="Loading keys…" />
        ) : (
          <Table columns={columns} rows={keys} rowKey={(k) => k.key_id} empty="No keys for this agent yet." />
        )}
      </CardBody>
    </Card>

      <CreateKeyModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(resp) => {
          setCreateOpen(false);
          setRevealKey(resp);
          reload();
        }}
        agentId={agentId}
      />

      <RawKeyModal value={revealKey} onClose={() => setRevealKey(null)} />

      <ConfirmDialog
        open={confirmRevoke !== null}
        onClose={() => setConfirmRevoke(null)}
        onConfirm={() => confirmRevoke && onRevoke(confirmRevoke)}
        title="Revoke this API key?"
        description="This cannot be undone."
        confirmLabel="Revoke Key"
        loading={revoking !== null}
      >
        {confirmRevoke && (
          <p className="text-sm text-muted">
            Key{' '}
            <span className="font-mono text-fg">{confirmRevoke.key_prefix}…</span>
            {confirmRevoke.name ? ` (${confirmRevoke.name})` : ''} will stop working immediately.
            Any agent or integration using it will start failing until it is replaced.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmRotate !== null}
        onClose={() => setConfirmRotate(null)}
        onConfirm={() => confirmRotate && onRotate(confirmRotate)}
        title="Rotate this API key?"
        description="A new secret is issued now."
        confirmLabel="Rotate Key"
        confirmVariant="primary"
        loading={rotating !== null}
      >
        {confirmRotate && (
          <p className="text-sm text-muted">
            A new secret is issued for{' '}
            <span className="font-mono text-fg">{confirmRotate.key_prefix}…</span>
            {confirmRotate.name ? ` (${confirmRotate.name})` : ''}. The old key keeps working during a
            short grace window, then stops. You&apos;ll see the new secret only once — copy it
            immediately.
          </p>
        )}
      </ConfirmDialog>
    </>
  );
}

function CreateKeyModal({
  open,
  onClose,
  onCreated,
  agentId,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (resp: CreateKeyResponse) => void;
  agentId: string;
}) {
  const [scopes, setScopes] = useState('agent:execute, llm:invoke, guardrails:check');
  const [name, setName] = useState('');
  const [expires, setExpires] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const resp = await createKey(agentId, {
        scopes: scopes
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        name: name.trim() || undefined,
        expires_in_days: expires.trim() ? Number(expires) : undefined,
      });
      onCreated(resp);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Issue API Key"
      description="The raw secret is returned once. Copy it now — it cannot be retrieved later."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="create-key-form" type="submit" loading={busy} disabled={!scopes.trim()}>
            Issue Key
          </Button>
        </>
      }
    >
      <form id="create-key-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Scopes" value={scopes} onChange={(e) => setScopes(e.target.value)} hint="Comma-separated." required />
        <Input label="Name (Optional)" value={name} onChange={(e) => setName(e.target.value)} />
        <Input
          label="Expires in Days (Optional)"
          type="number"
          min={1}
          value={expires}
          onChange={(e) => setExpires(e.target.value)}
        />
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

/** The raw-key-once modal — the ONLY place the secret is ever shown (issue AND rotate). */
function RawKeyModal({
  value,
  onClose,
}: {
  value: CreateKeyResponse | RotateKeyResponse | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  // A rotation response carries the previous-key grace fields; a fresh issue does not.
  const rotation = value && 'previous_key_expires_at' in value ? value : null;

  async function copy() {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value.api_key);
      setCopied(true);
      toast.success('Secret copied to clipboard.');
    } catch {
      toast.error('Clipboard unavailable — select and copy manually.');
    }
  }

  return (
    <Modal
      open={value !== null}
      onClose={onClose}
      closeOnBackdrop={false}
      title="Copy Your API Key Now"
      description="This is the only time the full secret is shown. Store it securely."
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
          <Button onClick={onClose}>I Have Stored It</Button>
        </>
      }
    >
      {value && (
        <div className="flex flex-col gap-3">
          {rotation && (
            <Callout tone="info" title="Previous Key Still Active">
              The previous key keeps working until {formatTime(rotation.previous_key_expires_at)}. Move
              your integrations to this new secret before then — after that, only this key works.
            </Callout>
          )}
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            The secret cannot be retrieved again. If you lose it, revoke this key and issue a new one.
          </div>
          <code className="block break-all rounded-md border border-border bg-surface-2 px-3 py-3 font-mono text-sm text-fg">
            {value.api_key}
          </code>
          <dl className="grid grid-cols-2 gap-2 text-xs text-muted">
            <div>
              <dt className="font-medium">Key ID</dt>
              <dd><CopyButton value={value.key_id} label="Copy Key ID" /></dd>
            </div>
            <div>
              <dt className="font-medium">Prefix</dt>
              <dd className="font-mono text-fg">{value.key_prefix}</dd>
            </div>
          </dl>
        </div>
      )}
    </Modal>
  );
}

export default function KeysPage() {
  return (
    <Suspense fallback={<Loading />}>
      <KeysInner />
    </Suspense>
  );
}
