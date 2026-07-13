'use client';

import { useEffect, useState, type FormEvent } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  CopyButton,
  EmptyState,
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
import { BffError } from '@/lib/bff-client';
import {
  createWebhook,
  deleteWebhook,
  listWebhookDeliveries,
  listWebhooks,
  replayWebhook,
  resumeWebhook,
  rotateWebhookSecret,
} from '@/lib/services';
import type { Webhook, WebhookDelivery } from '@/lib/types';
import { formatTime } from '@/lib/utils';

/**
 * Webhooks management (auth `/v1/webhooks`, tenant:admin). The signing secret is surfaced
 * exactly ONCE — on create and on rotate — via a reveal modal; it is never re-fetchable.
 *
 * Wire-shape note: the Auth WebhookController emits `sub_id` / `signing_secret` /
 * `last_status_code`, while `@/lib/types` names them `id` / `secret` / `response_status`.
 * Both carry index signatures, so we read tolerantly (typed name first, raw backend key as a
 * fallback) — the screen works whether the BFF normalizes the payload or passes it through raw.
 */

function pickString(row: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const k of keys) {
    const v = row[k];
    if (typeof v === 'string' && v.length > 0) return v;
  }
  return undefined;
}

function pickNumber(row: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const k of keys) {
    const v = row[k];
    if (typeof v === 'number' && Number.isFinite(v)) return v;
  }
  return undefined;
}

/** Stable id used for every per-webhook route (delete/rotate/resume/replay/deliveries + copy). */
const webhookId = (w: Webhook): string => pickString(w, 'id', 'sub_id') ?? '';
/** The one-time signing secret, present only on a create/rotate response. */
const webhookSecret = (w: Webhook): string | undefined => pickString(w, 'secret', 'signing_secret');
const deliveryKey = (d: WebhookDelivery): string | undefined => pickString(d, 'delivery_id', 'id');
const deliveryHttpStatus = (d: WebhookDelivery): number | undefined =>
  pickNumber(d, 'response_status', 'last_status_code');

const isResumable = (w: Webhook): boolean => {
  const s = (w.status ?? '').toLowerCase();
  return s === 'paused' || s === 'disabled';
};

/** Event-type chips: `['*']` (or any list containing `*`) collapses to a single "All Events" badge. */
function EventTypes({ types }: { types: string[] }) {
  if (types.length === 0) return <span className="text-xs text-muted">—</span>;
  if (types.includes('*')) return <Badge tone="info">All Events</Badge>;
  const shown = types.slice(0, 4);
  const extra = types.length - shown.length;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((t) => (
        <Badge key={t}>{t}</Badge>
      ))}
      {extra > 0 && <span className="text-xs text-muted">+{extra} more</span>}
    </div>
  );
}

export default function WebhooksPage() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listWebhooks(signal), []);
  const webhooks = data ?? [];

  const [createOpen, setCreateOpen] = useState(false);
  // The reveal modal is shared by BOTH create and rotate — either flow returns the secret once.
  const [revealSecret, setRevealSecret] = useState<Webhook | null>(null);
  const [confirmRotate, setConfirmRotate] = useState<Webhook | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Webhook | null>(null);
  const [deliveriesFor, setDeliveriesFor] = useState<Webhook | null>(null);
  const [rotatingId, setRotatingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [resumingId, setResumingId] = useState<string | null>(null);

  const forbidden = error instanceof BffError && error.status === 403;

  function actionError(err: unknown, fallback: string) {
    toast.error(err instanceof Error ? err.message : fallback);
  }

  async function onRotate(w: Webhook) {
    const id = webhookId(w);
    setRotatingId(id);
    try {
      const resp = await rotateWebhookSecret(id);
      setConfirmRotate(null);
      setRevealSecret(resp); // surface the new secret ONCE via the shared reveal modal
      reload();
    } catch (err) {
      actionError(err, 'Could not rotate the signing secret.');
    } finally {
      setRotatingId(null);
    }
  }

  async function onDelete(w: Webhook) {
    const id = webhookId(w);
    setDeletingId(id);
    try {
      await deleteWebhook(id);
      toast.success('Webhook deleted.');
      setConfirmDelete(null);
      reload();
    } catch (err) {
      actionError(err, 'Could not delete the webhook.');
    } finally {
      setDeletingId(null);
    }
  }

  async function onResume(w: Webhook) {
    const id = webhookId(w);
    setResumingId(id);
    try {
      await resumeWebhook(id);
      toast.success('Webhook resumed.');
      reload();
    } catch (err) {
      actionError(err, 'Could not resume the webhook.');
    } finally {
      setResumingId(null);
    }
  }

  const columns: Array<Column<Webhook>> = [
    {
      key: 'url',
      header: 'URL',
      render: (w) => (
        <span className="block max-w-[320px] truncate font-mono text-xs text-fg" title={w.url}>
          {w.url}
        </span>
      ),
    },
    {
      key: 'event_types',
      header: 'Event Types',
      render: (w) => <EventTypes types={w.event_types ?? []} />,
    },
    { key: 'status', header: 'Status', render: (w) => <StatusBadge status={w.status} /> },
    {
      key: 'created',
      header: 'Created',
      render: (w) => <span className="text-xs text-muted">{formatTime(w.created_at)}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (w) => {
        const id = webhookId(w);
        return (
          <div className="flex items-center justify-end gap-1.5">
            <Button variant="ghost" size="sm" onClick={() => setDeliveriesFor(w)}>
              Deliveries
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setConfirmRotate(w)}>
              Rotate Secret
            </Button>
            {isResumable(w) && (
              <Button
                variant="secondary"
                size="sm"
                loading={resumingId === id}
                onClick={() => {
                  void onResume(w);
                }}
              >
                Resume
              </Button>
            )}
            <Button variant="danger" size="sm" onClick={() => setConfirmDelete(w)}>
              Delete
            </Button>
            <CopyButton value={id} label="Copy Webhook ID" />
          </div>
        );
      },
    },
  ];

  return (
    <Page>
      <PageHeader
        title="Webhooks"
        description="Manage outbound webhook subscriptions and inspect their delivery history."
        actions={<Button onClick={() => setCreateOpen(true)}>Add Webhook</Button>}
      />

      <PageBody fill>
        <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <CardHeader
            title="Subscriptions"
            description={
              webhooks.length > 0
                ? `${webhooks.length} endpoint${webhooks.length === 1 ? '' : 's'}`
                : undefined
            }
            actions={
              <Button variant="secondary" size="md" onClick={reload} disabled={loading}>
                Refresh
              </Button>
            }
          />
          <CardBody className="min-h-0 flex-1 overflow-y-auto p-0">
            {loading ? (
              <Loading label="Loading webhooks…" />
            ) : forbidden ? (
              <div className="p-4">
                <Callout tone="warning" title="Admin Access Required">
                  Managing webhooks requires the <span className="font-mono">tenant:admin</span> scope.
                  Ask a workspace administrator to grant it, then reload this page.
                </Callout>
              </div>
            ) : error ? (
              <div className="p-4">
                <ErrorBanner error={error} title="Could not load webhooks" />
              </div>
            ) : webhooks.length === 0 ? (
              <div className="p-6">
                <EmptyState
                  title="No webhooks yet"
                  description="Add a subscription to receive event notifications at your own https endpoint."
                  action={<Button onClick={() => setCreateOpen(true)}>Add Webhook</Button>}
                />
              </div>
            ) : (
              <Table columns={columns} rows={webhooks} rowKey={(w, i) => webhookId(w) || String(i)} />
            )}
          </CardBody>
        </Card>
      </PageBody>

      <AddWebhookModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setCreateOpen(false);
          setRevealSecret(created); // show the signing secret ONCE
          reload();
        }}
      />

      <RawSecretModal webhook={revealSecret} onClose={() => setRevealSecret(null)} />

      {deliveriesFor && (
        <DeliveriesModal webhook={deliveriesFor} onClose={() => setDeliveriesFor(null)} />
      )}

      <ConfirmDialog
        open={confirmRotate !== null}
        onClose={() => setConfirmRotate(null)}
        onConfirm={() => confirmRotate && void onRotate(confirmRotate)}
        title="Rotate this webhook's signing secret?"
        description="A new signing secret is issued now."
        confirmLabel="Rotate Secret"
        confirmVariant="primary"
        loading={rotatingId !== null}
      >
        {confirmRotate && (
          <p className="text-sm text-muted">
            The current signing secret for{' '}
            <span className="font-mono text-fg">{confirmRotate.url}</span> stops working and a new one
            is issued. You&apos;ll see the new secret only once — copy it immediately and update your
            endpoint&apos;s signature verification.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => confirmDelete && void onDelete(confirmDelete)}
        title="Delete this webhook?"
        description="This cannot be undone."
        confirmLabel="Delete Webhook"
        loading={deletingId !== null}
      >
        {confirmDelete && (
          <p className="text-sm text-muted">
            Deliveries to <span className="font-mono text-fg">{confirmDelete.url}</span> stop
            immediately and the subscription&apos;s delivery history is removed.
          </p>
        )}
      </ConfirmDialog>
    </Page>
  );
}

/** Add-webhook form: an https endpoint + comma-separated event types (`*` = all events). */
function AddWebhookModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (created: Webhook) => void;
}) {
  const [url, setUrl] = useState('');
  const [eventTypes, setEventTypes] = useState('*');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Reset the form each time the modal reopens so a prior entry can't leak in.
  useEffect(() => {
    if (open) {
      setUrl('');
      setEventTypes('*');
      setError(null);
      setFormError(null);
    }
  }, [open]);

  const parsedTypes = eventTypes
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  const urlOk = /^https:\/\//i.test(url.trim());
  const canSubmit = urlOk && parsedTypes.length > 0;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!urlOk) {
      setFormError('Enter a valid https:// URL.');
      return;
    }
    if (parsedTypes.length === 0) {
      setFormError('Add at least one event type (or * for all events).');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createWebhook({ url: url.trim(), event_types: parsedTypes });
      onCreated(created);
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
      title="Add Webhook"
      description="The signing secret is returned once, right after creation. Copy it then — it cannot be retrieved later."
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button form="add-webhook-form" type="submit" loading={busy} disabled={!canSubmit}>
            Create Webhook
          </Button>
        </>
      }
    >
      <form id="add-webhook-form" onSubmit={submit} className="flex flex-col gap-4">
        <Input
          label="Endpoint URL"
          type="url"
          inputMode="url"
          placeholder="https://example.com/webhooks/cypherx"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          hint="Must be an https URL. Events are POSTed here, signed with the signing secret."
          required
        />
        <div className="flex flex-col gap-1.5">
          <Input
            label="Event Types"
            placeholder="task.completed, task.failed"
            value={eventTypes}
            onChange={(e) => setEventTypes(e.target.value)}
            hint="Comma-separated event types. Use * to subscribe to all events."
          />
          {parsedTypes.length > 0 && (
            <div className="flex flex-wrap items-center gap-1">
              {parsedTypes.includes('*') ? (
                <Badge tone="info">All Events</Badge>
              ) : (
                parsedTypes.map((t) => <Badge key={t}>{t}</Badge>)
              )}
            </div>
          )}
        </div>
        {formError && <p className="text-xs text-danger">{formError}</p>}
        {error ? <ErrorBanner error={error} /> : null}
      </form>
    </Modal>
  );
}

/**
 * The one-time signing-secret reveal — the ONLY place the secret is ever shown (create AND
 * rotate). Once dismissed it is gone; the read endpoints never return it.
 */
function RawSecretModal({ webhook, onClose }: { webhook: Webhook | null; onClose: () => void }) {
  const secret = webhook ? webhookSecret(webhook) : undefined;
  return (
    <Modal
      open={webhook !== null}
      onClose={onClose}
      closeOnBackdrop={false}
      title="Copy Your Signing Secret Now"
      description="This is the only time the signing secret is shown. Store it securely."
      size="md"
      footer={<Button onClick={onClose}>I Have Stored It</Button>}
    >
      {webhook && (
        <div className="flex flex-col gap-3">
          <Callout tone="warning" title="Store This Now">
            Store this now — it won&apos;t be shown again. Use it to verify the signature on every
            event delivered to your endpoint.
          </Callout>
          {secret ? (
            <>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-muted">Signing secret</span>
                <CopyButton value={secret} label="Copy Signing Secret" />
              </div>
              <code className="block break-all rounded-md border border-border bg-surface-2 px-3 py-3 font-mono text-sm text-fg">
                {secret}
              </code>
            </>
          ) : (
            <Callout tone="danger" title="No Secret Returned">
              The server did not return a signing secret. Rotate the secret to issue a new one.
            </Callout>
          )}
          <dl className="text-xs text-muted">
            <div className="flex items-center justify-between gap-3">
              <dt className="font-medium">Endpoint</dt>
              <dd className="min-w-0 truncate font-mono text-fg" title={webhook.url}>
                {webhook.url}
              </dd>
            </div>
          </dl>
        </div>
      )}
    </Modal>
  );
}

/** Delivery history for one webhook, with per-row replay and a "replay recent failures" action. */
function DeliveriesModal({ webhook, onClose }: { webhook: Webhook; onClose: () => void }) {
  const toast = useToast();
  const id = webhookId(webhook);
  const { data, loading, error, reload } = useAsync(
    (signal) => listWebhookDeliveries(id, signal),
    [id],
  );
  const deliveries = data ?? [];
  const [replayingAll, setReplayingAll] = useState(false);
  const [replayingId, setReplayingId] = useState<string | null>(null);

  async function replayAll() {
    setReplayingAll(true);
    try {
      const res = await replayWebhook(id);
      const replayed = res?.replayed;
      const n = typeof replayed === 'number' ? replayed : null;
      toast.success(
        n === null
          ? 'Recent failures re-queued for delivery.'
          : n === 0
            ? 'No failed deliveries to replay.'
            : `Re-queued ${n} failed ${n === 1 ? 'delivery' : 'deliveries'}.`,
      );
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not replay recent failures.');
    } finally {
      setReplayingAll(false);
    }
  }

  async function replayOne(deliveryId: string) {
    setReplayingId(deliveryId);
    try {
      await replayWebhook(id, deliveryId);
      toast.success('Delivery re-queued.');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not replay this delivery.');
    } finally {
      setReplayingId(null);
    }
  }

  const columns: Array<Column<WebhookDelivery>> = [
    {
      key: 'event_type',
      header: 'Event Type',
      render: (d) => <span className="font-mono text-xs text-fg">{d.event_type ?? '—'}</span>,
    },
    { key: 'status', header: 'Status', render: (d) => <StatusBadge status={d.status} /> },
    {
      key: 'http',
      header: 'HTTP',
      className: 'text-right',
      render: (d) => {
        const code = deliveryHttpStatus(d);
        return (
          <span className="font-mono text-xs tabular-nums text-muted">{code ?? '—'}</span>
        );
      },
    },
    {
      key: 'attempts',
      header: 'Attempts',
      className: 'text-right',
      render: (d) => (
        <span className="font-mono text-xs tabular-nums text-muted">{d.attempts ?? '—'}</span>
      ),
    },
    {
      key: 'created',
      header: 'Created',
      render: (d) => <span className="text-xs text-muted">{formatTime(d.created_at)}</span>,
    },
    {
      key: 'delivered',
      header: 'Delivered',
      render: (d) => <span className="text-xs text-muted">{formatTime(d.delivered_at)}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (d) => {
        const dk = deliveryKey(d);
        return (
          <Button
            variant="secondary"
            size="sm"
            disabled={!dk}
            loading={replayingId !== null && replayingId === dk}
            onClick={() => {
              if (dk) void replayOne(dk);
            }}
          >
            Replay
          </Button>
        );
      },
    },
  ];

  return (
    <Modal
      open
      onClose={onClose}
      title="Webhook Deliveries"
      description={
        <span className="block max-w-full truncate font-mono text-xs" title={webhook.url}>
          {webhook.url}
        </span>
      }
      size="lg"
      footer={
        <Button variant="secondary" onClick={onClose}>
          Close
        </Button>
      }
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-xs text-muted">Delivery attempts, newest first.</p>
        <Button
          variant="secondary"
          size="sm"
          loading={replayingAll}
          onClick={() => {
            void replayAll();
          }}
        >
          Replay Recent Failures
        </Button>
      </div>

      {loading ? (
        <Loading label="Loading deliveries…" />
      ) : error ? (
        <ErrorBanner error={error} title="Could not load deliveries" />
      ) : (
        <div className="max-h-[52vh] overflow-y-auto rounded-md border border-border">
          <Table
            columns={columns}
            rows={deliveries}
            rowKey={(d, i) => deliveryKey(d) ?? String(i)}
            empty="No deliveries recorded for this webhook yet."
          />
        </div>
      )}
    </Modal>
  );
}
