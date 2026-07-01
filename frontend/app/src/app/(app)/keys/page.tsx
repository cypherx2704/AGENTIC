'use client';

import { Suspense, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { PageHeader } from '@/components/AppShell';
import { AgentList } from '@/components/AgentList';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { createKey, listKeys, revokeKey } from '@/lib/services';
import type { ApiKeyListItem, CreateKeyResponse } from '@/lib/types';
import { formatTime, shortId } from '@/lib/utils';

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
      <div>
        <PageHeader
          title="API keys"
          description="Issue and revoke this agent's API keys. The raw secret is shown exactly once."
          actions={
            <Link href="/keys" className="text-sm text-brand hover:underline">
              ← All agents
            </Link>
          }
        />
        <KeyManager agentId={agentId} toastError={toast.error} />
      </div>
    );
  }

  // No agent selected -> pick one from the tenant's agents (or paste an id as a fallback).
  return (
    <div>
      <PageHeader title="API keys" description="Choose an agent to manage its API keys." />
      <AgentList
        onSelect={(a) => selectAgent(a.agent_id)}
        actionLabel="Manage keys"
        emptyLabel="No agents yet — create one on the Agents page first."
        fallback={<ManageById onSubmit={selectAgent} />}
      />
    </div>
  );
}

/** Fallback: jump straight to an agent's keys by pasting its id (for agents beyond the loaded pages). */
function ManageById({ onSubmit }: { onSubmit: (id: string) => void }) {
  const [id, setId] = useState('');
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (id.trim()) onSubmit(id.trim());
      }}
      className="flex items-end gap-2"
    >
      <Input placeholder="agent id…" value={id} onChange={(e) => setId(e.target.value)} className="w-72" />
      <Button type="submit" size="sm" variant="secondary" disabled={!id.trim()}>
        Manage keys
      </Button>
    </form>
  );
}

function KeyManager({ agentId, toastError }: { agentId: string; toastError: (m: string) => void }) {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listKeys(agentId, signal), [agentId]);
  const [createOpen, setCreateOpen] = useState(false);
  const [revealKey, setRevealKey] = useState<CreateKeyResponse | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<ApiKeyListItem | null>(null);

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
    { key: 'last_used', header: 'Last used', render: (k) => <span className="text-xs text-muted">{formatTime(k.last_used_at)}</span> },
    {
      key: 'actions',
      header: '',
      render: (k) =>
        k.status === 'active' ? (
          <Button
            variant="danger"
            size="sm"
            loading={revoking === k.key_id}
            onClick={() => setConfirmRevoke(k)}
          >
            Revoke
          </Button>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Keys"
        description={<span className="font-mono text-xs">{agentId}</span>}
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            New key
          </Button>
        }
      />
      <CardBody className="px-0 py-0">
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
        confirmLabel="Revoke key"
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
    </Card>
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
      title="Issue API key"
      description="The raw secret is returned once. Copy it now — it cannot be retrieved later."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="create-key-form" type="submit" loading={busy} disabled={!scopes.trim()}>
            Issue key
          </Button>
        </>
      }
    >
      <form id="create-key-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input label="Scopes" value={scopes} onChange={(e) => setScopes(e.target.value)} hint="Comma-separated." required />
        <Input label="Name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
        <Input
          label="Expires in days (optional)"
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

/** The raw-key-once modal — the ONLY place the secret is ever shown. */
function RawKeyModal({ value, onClose }: { value: CreateKeyResponse | null; onClose: () => void }) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);

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
      title="Copy your API key now"
      description="This is the only time the full secret is shown. Store it securely."
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={copy}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
          <Button onClick={onClose}>I have stored it</Button>
        </>
      }
    >
      {value && (
        <div className="flex flex-col gap-3">
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            The secret cannot be retrieved again. If you lose it, revoke this key and issue a new one.
          </div>
          <code className="block break-all rounded-md border border-border bg-surface-2 px-3 py-3 font-mono text-sm text-fg">
            {value.api_key}
          </code>
          <dl className="grid grid-cols-2 gap-2 text-xs text-muted">
            <div>
              <dt className="font-medium">Key ID</dt>
              <dd className="font-mono text-fg">{shortId(value.key_id, 16)}</dd>
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
