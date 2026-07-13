'use client';

import { useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { useSession } from '@/components/SessionProvider';
import { ScopeSelector } from '@/components/ScopeSelector';
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
  Field,
  Input,
  Loading,
  Modal,
  StatusBadge,
  Table,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import {
  createServiceClient,
  deleteServiceClient,
  emergencyRotateSigningKey,
  listServiceClients,
  listSigningKeys,
  rotateServiceClientSecret,
  rotateSigningKey,
} from '@/lib/services';
import type { ServiceClientView, SigningKeyView } from '@/lib/types';
import { cn, formatTime } from '@/lib/utils';

// ── Field readers (shape-tolerant) ────────────────────────────────────────────────────────
// The frontend `ServiceClientView` documents `id`/`scopes`/`secret`, but the auth service emits
// `client_id`/`allowed_scopes`/`client_secret` on the wire (both admitted by the type's index
// signature). Read defensively so the copy-id action, scope chips, and — most importantly — the
// shown-once secret never silently come back empty against the real gateway.
function clientIdOf(c: ServiceClientView): string | undefined {
  return c.id ?? c.client_id;
}
function scopesOf(c: ServiceClientView): string[] {
  if (Array.isArray(c.scopes)) return c.scopes;
  const raw = c.allowed_scopes;
  return Array.isArray(raw) ? raw.filter((s): s is string => typeof s === 'string') : [];
}
function secretOf(c: ServiceClientView): string | undefined {
  if (typeof c.secret === 'string' && c.secret) return c.secret;
  const raw = c.client_secret;
  return typeof raw === 'string' && raw ? raw : undefined;
}

type TabKey = 'signing-keys' | 'service-clients';

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'signing-keys', label: 'Signing Keys' },
  { key: 'service-clients', label: 'Service Clients' },
];

export default function AdminPage() {
  const { session } = useSession();
  const [tab, setTab] = useState<TabKey>('signing-keys');

  const scopes = session?.scopes ?? [];
  const isPlatformAdmin = scopes.includes('platform:admin');

  return (
    <Page>
      <PageHeader
        title="Platform Admin"
        description="Manage JWT signing keys and OAuth2 service clients — platform-admin-only operations."
      />
      <PageBody>
        {!isPlatformAdmin ? (
          <Callout tone="warning" title="Platform Admin Only">
            You need the platform:admin scope to manage signing keys and service clients.
          </Callout>
        ) : (
          <div>
            <div className="mb-4 flex items-center gap-1 border-b border-border">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setTab(t.key)}
                  aria-current={tab === t.key ? 'page' : undefined}
                  className={cn(
                    'relative -mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors',
                    tab === t.key
                      ? 'border-brand text-fg-strong'
                      : 'border-transparent text-muted hover:text-fg',
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {tab === 'signing-keys' ? <SigningKeysTab /> : null}
            {tab === 'service-clients' ? <ServiceClientsTab availableScopes={scopes} /> : null}
          </div>
        )}
      </PageBody>
    </Page>
  );
}

// ── Signing Keys tab ───────────────────────────────────────────────────────────────────────
function SigningKeysTab() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listSigningKeys(signal), []);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [confirmEmergency, setConfirmEmergency] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [emergencyRotating, setEmergencyRotating] = useState(false);
  const [emergencyToken, setEmergencyToken] = useState('');

  const keys = data ?? [];

  async function onRotate() {
    setRotating(true);
    try {
      await rotateSigningKey();
      toast.success('Signing key rotated. The previous key stays valid for in-flight verification.');
      setConfirmRotate(false);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not rotate the signing key.');
    } finally {
      setRotating(false);
    }
  }

  async function onEmergencyRotate() {
    if (!emergencyToken.trim()) {
      toast.error('Enter the emergency rotation token to proceed.');
      return;
    }
    setEmergencyRotating(true);
    try {
      await emergencyRotateSigningKey(emergencyToken.trim());
      toast.success('Emergency rotation complete. Tokens signed by the old key are now rejected.');
      setConfirmEmergency(false);
      setEmergencyToken('');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not perform the emergency rotation.');
    } finally {
      setEmergencyRotating(false);
    }
  }

  const columns: Array<Column<SigningKeyView>> = [
    {
      key: 'kid',
      header: 'Key ID',
      render: (k) => <span className="font-mono text-xs text-fg">{k.kid ?? k.key_id ?? '—'}</span>,
    },
    { key: 'status', header: 'Status', render: (k) => <StatusBadge status={k.status} /> },
    {
      key: 'algorithm',
      header: 'Algorithm',
      render: (k) => <span className="font-mono text-xs text-fg">{k.algorithm ?? '—'}</span>,
    },
    {
      key: 'created',
      header: 'Created',
      render: (k) => <span className="text-xs text-muted">{formatTime(k.created_at)}</span>,
    },
    {
      key: 'not_after',
      header: 'Not After',
      render: (k) => <span className="text-xs text-muted">{formatTime(k.not_after)}</span>,
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Signing Keys"
        description="JWT signing keys used to mint access tokens. Exactly one key is active at a time."
        actions={
          <>
            <Button size="sm" variant="secondary" onClick={() => setConfirmRotate(true)}>
              Rotate
            </Button>
            <Button size="sm" variant="danger" onClick={() => setConfirmEmergency(true)}>
              Emergency Rotate
            </Button>
          </>
        }
      />
      <CardBody className="px-0 py-0">
        {error ? (
          <div className="p-4">
            <ErrorBanner error={error} title="Could not load signing keys" />
          </div>
        ) : loading ? (
          <Loading label="Loading signing keys…" />
        ) : (
          <Table
            columns={columns}
            rows={keys}
            rowKey={(k, i) => k.kid ?? k.key_id ?? String(i)}
            empty="No signing keys found."
          />
        )}
      </CardBody>

      <ConfirmDialog
        open={confirmRotate}
        onClose={() => setConfirmRotate(false)}
        onConfirm={onRotate}
        loading={rotating}
        title="Rotate Signing Key"
        description="Promote the staged next key to active."
        confirmLabel="Rotate Key"
        confirmVariant="primary"
      >
        <p className="text-sm text-muted">
          A new signing key becomes active. The previous key stays in a verifying state, so tokens it
          already signed keep validating until they expire — no live sessions are interrupted. This is
          the routine, graceful rotation.
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmEmergency}
        onClose={() => {
          setConfirmEmergency(false);
          setEmergencyToken('');
        }}
        onConfirm={onEmergencyRotate}
        loading={emergencyRotating}
        title="Emergency Rotate Signing Key"
        description="Immediate rotation for a suspected key compromise."
        confirmLabel="Emergency Rotate"
        confirmVariant="danger"
      >
        <Callout tone="danger" title="This Invalidates Live Tokens">
          The old signing key is poisoned immediately. Every access token it ever signed is rejected at
          once across all services — active agents and integrations will fail until they
          re-authenticate. Use this only when the key may be compromised.
        </Callout>
        <div className="mt-3">
          <Input
            label="Emergency Rotation Token"
            type="password"
            value={emergencyToken}
            onChange={(e) => setEmergencyToken(e.target.value)}
            placeholder="Out-of-band emergency token"
            hint="This action is gated by a separately-held emergency token, sent as X-Emergency-Token."
            autoComplete="off"
          />
        </div>
      </ConfirmDialog>
    </Card>
  );
}

// ── Service Clients tab ────────────────────────────────────────────────────────────────────
function ServiceClientsTab({ availableScopes }: { availableScopes: readonly string[] }) {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listServiceClients(signal), []);
  const [createOpen, setCreateOpen] = useState(false);
  const [reveal, setReveal] = useState<{ secret: string; name: string } | null>(null);
  const [confirmRotate, setConfirmRotate] = useState<ServiceClientView | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<ServiceClientView | null>(null);
  const [rotating, setRotating] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const clients = data ?? [];

  async function onRotateSecret() {
    const target = confirmRotate;
    const id = target && clientIdOf(target);
    if (!target || !id) return;
    setRotating(true);
    try {
      const updated = await rotateServiceClientSecret(id);
      setConfirmRotate(null);
      const secret = secretOf(updated);
      if (secret) {
        setReveal({ secret, name: target.name ?? 'this client' });
      } else {
        toast.success('Secret rotated.');
      }
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not rotate the secret.');
    } finally {
      setRotating(false);
    }
  }

  async function onDelete() {
    const target = confirmDelete;
    const id = target && clientIdOf(target);
    if (!target || !id) return;
    setDeleting(true);
    try {
      await deleteServiceClient(id);
      toast.success('Service client deleted.');
      setConfirmDelete(null);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not delete the service client.');
    } finally {
      setDeleting(false);
    }
  }

  const columns: Array<Column<ServiceClientView>> = [
    {
      key: 'name',
      header: 'Name',
      render: (c) => <span className="font-medium text-fg">{c.name ?? '—'}</span>,
    },
    {
      key: 'service',
      header: 'Service Name',
      render: (c) =>
        c.service_name ? (
          <span className="text-sm text-fg">{c.service_name}</span>
        ) : (
          <span className="text-muted">—</span>
        ),
    },
    {
      key: 'scopes',
      header: 'Scopes',
      render: (c) => {
        const scopes = scopesOf(c);
        return scopes.length ? (
          <div className="flex flex-wrap gap-1">
            {scopes.map((s) => (
              <Badge key={s}>{s}</Badge>
            ))}
          </div>
        ) : (
          <span className="text-muted">—</span>
        );
      },
    },
    { key: 'status', header: 'Status', render: (c) => <StatusBadge status={c.status} /> },
    {
      key: 'created',
      header: 'Created',
      render: (c) => <span className="text-xs text-muted">{formatTime(c.created_at)}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (c) => {
        const id = clientIdOf(c);
        return (
          <div className="flex items-center justify-end gap-2">
            {id ? <CopyButton value={id} label="Copy Client ID" /> : null}
            <Button size="sm" variant="secondary" disabled={!id} onClick={() => setConfirmRotate(c)}>
              Rotate Secret
            </Button>
            <Button size="sm" variant="danger" disabled={!id} onClick={() => setConfirmDelete(c)}>
              Delete
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Service Clients"
        description="OAuth2 client-credentials principals for machine-to-machine access."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            New Service Client
          </Button>
        }
      />
      <CardBody className="px-0 py-0">
        {error ? (
          <div className="p-4">
            <ErrorBanner error={error} title="Could not load service clients" />
          </div>
        ) : loading ? (
          <Loading label="Loading service clients…" />
        ) : (
          <Table
            columns={columns}
            rows={clients}
            rowKey={(c, i) => clientIdOf(c) ?? String(i)}
            empty="No service clients yet."
          />
        )}
      </CardBody>

      {createOpen ? (
        <CreateServiceClientModal
          availableScopes={availableScopes}
          onClose={() => setCreateOpen(false)}
          onCreated={(client, fallbackName) => {
            setCreateOpen(false);
            const secret = secretOf(client);
            if (secret) {
              setReveal({ secret, name: client.name ?? fallbackName });
            } else {
              toast.success('Service client created.');
            }
            reload();
          }}
        />
      ) : null}

      <SecretRevealModal reveal={reveal} onClose={() => setReveal(null)} />

      <ConfirmDialog
        open={confirmRotate !== null}
        onClose={() => setConfirmRotate(null)}
        onConfirm={onRotateSecret}
        loading={rotating}
        title="Rotate Client Secret"
        description="Issue a new secret for this service client."
        confirmLabel="Rotate Secret"
        confirmVariant="primary"
      >
        {confirmRotate && (
          <p className="text-sm text-muted">
            A new secret is minted for{' '}
            <span className="font-medium text-fg">{confirmRotate.name ?? 'this client'}</span> and the
            old one stops working immediately. You&apos;ll see the new secret only once — copy it before
            closing the dialog.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        onConfirm={onDelete}
        loading={deleting}
        title="Delete Service Client"
        description="This cannot be undone."
        confirmLabel="Delete"
      >
        {confirmDelete && (
          <p className="text-sm text-muted">
            <span className="font-medium text-fg">{confirmDelete.name ?? 'This client'}</span> will stop
            authenticating immediately. Any integration using its credentials will start failing.
          </p>
        )}
      </ConfirmDialog>
    </Card>
  );
}

// ── New Service Client modal ────────────────────────────────────────────────────────────────
function CreateServiceClientModal({
  availableScopes,
  onClose,
  onCreated,
}: {
  availableScopes: readonly string[];
  onClose: () => void;
  onCreated: (client: ServiceClientView, fallbackName: string) => void;
}) {
  const [name, setName] = useState('');
  const [serviceName, setServiceName] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createServiceClient({
        name: name.trim(),
        service_name: serviceName.trim() || undefined,
        scopes,
      });
      onCreated(created, name.trim());
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="New Service Client"
      description="Register an OAuth2 service client. The client secret is returned once on creation."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            form="create-service-client-form"
            type="submit"
            loading={busy}
            disabled={!name.trim() || scopes.length === 0}
          >
            Create Client
          </Button>
        </>
      }
    >
      <form id="create-service-client-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. billing-sync"
          required
        />
        <Input
          label="Service Name (Optional)"
          value={serviceName}
          onChange={(e) => setServiceName(e.target.value)}
          placeholder="e.g. billing"
        />
        <Field label="Scopes" hint="The client can be granted any scope you currently hold.">
          <ScopeSelector available={availableScopes} value={scopes} onChange={setScopes} />
        </Field>
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

// ── Shown-once secret reveal ────────────────────────────────────────────────────────────────
function SecretRevealModal({
  reveal,
  onClose,
}: {
  reveal: { secret: string; name: string } | null;
  onClose: () => void;
}) {
  return (
    <Modal
      open={reveal !== null}
      onClose={onClose}
      closeOnBackdrop={false}
      title="Copy the Client Secret Now"
      description="This is the only time the full secret is shown. Store it somewhere safe."
      size="md"
      footer={<Button onClick={onClose}>I Have Stored It</Button>}
    >
      {reveal && (
        <div className="flex flex-col gap-3">
          <Callout tone="warning" title="Won't Be Shown Again">
            The secret for <span className="font-medium text-fg">{reveal.name}</span> cannot be
            retrieved later. If you lose it, rotate the secret to issue a new one.
          </Callout>
          <code className="block break-all rounded-md border border-border bg-surface-2 px-3 py-3 font-mono text-sm text-fg">
            {reveal.secret}
          </code>
          <div className="flex justify-end">
            <CopyButton value={reveal.secret} label="Copy Secret" />
          </div>
        </div>
      )}
    </Modal>
  );
}
