'use client';

import { useState } from 'react';
import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  ConfirmDialog,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  Select,
  Switch,
  Table,
  humanizeStatus,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { BffError } from '@/lib/bff-client';
import { addKbAcl, deleteKbAcl, listKbAcls } from '@/lib/services';
import { useAsync } from '@/lib/useAsync';
import type { RagAcl, RagPermission, RagPrincipalType } from '@/lib/types';
import { formatTime } from '@/lib/utils';

const PRINCIPAL_TYPES: RagPrincipalType[] = ['agent', 'api_key', 'user', 'role', 'tenant'];
const PERMISSIONS: RagPermission[] = ['read', 'query', 'ingest', 'write', 'admin'];

/** Row identity — an ACL grant is unique per (principal_type, principal_id). */
function aclKey(a: RagAcl): string {
  return `${a.principal_type}:${a.principal_id}`;
}

export function AclTab({ kbId }: { kbId: string }) {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync((signal) => listKbAcls(kbId, signal), [kbId]);

  const [addOpen, setAddOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<RagAcl | null>(null);
  const [deleting, setDeleting] = useState(false);

  const acls = data?.acls ?? [];
  // ACL management needs rag:admin + the `admin` permission on the KB; a 403 is expected, not a crash.
  const isForbidden = error instanceof BffError && error.status === 403;

  async function onDelete(acl: RagAcl) {
    setDeleting(true);
    try {
      await deleteKbAcl(kbId, acl.principal_type, acl.principal_id);
      toast.success('Access grant removed.');
      setConfirmDelete(null);
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Delete failed.');
    } finally {
      setDeleting(false);
    }
  }

  const columns: Array<Column<RagAcl>> = [
    {
      key: 'principal_type',
      header: 'Principal Type',
      render: (a) => <Badge>{humanizeStatus(a.principal_type)}</Badge>,
    },
    {
      key: 'principal_id',
      header: 'Principal ID',
      render: (a) => (
        <span className="font-mono text-xs text-fg">
          {a.principal_id}
          {a.principal_id === '*' ? <span className="ml-1.5 font-sans text-muted">all principals</span> : null}
        </span>
      ),
    },
    {
      key: 'permissions',
      header: 'Permissions',
      render: (a) =>
        a.permissions.length === 0 ? (
          <span className="text-muted">—</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {a.permissions.map((p) => (
              <Badge key={p} tone={p === 'admin' ? 'warning' : 'neutral'}>
                {p}
              </Badge>
            ))}
          </div>
        ),
    },
    {
      key: 'expires',
      header: 'Expires',
      render: (a) => <span className="text-xs text-muted">{a.expires_at ? formatTime(a.expires_at) : 'Never'}</span>,
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (a) => (
        <Button variant="danger" size="sm" onClick={() => setConfirmDelete(a)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Access Control"
        description="Grant principals access to this knowledge base. A KB with no grants is readable by no one."
        actions={
          <Button size="md" onClick={() => setAddOpen(true)}>
            Add Grant
          </Button>
        }
      />
      <CardBody className="px-0 py-0">
        {error ? (
          <div className="flex flex-col gap-3 p-4">
            <ErrorBanner error={error} title="Could not load access grants" />
            {isForbidden ? (
              <Callout tone="warning" title="Access Management Restricted">
                Managing access needs the <span className="font-mono">rag:admin</span> scope and the{' '}
                <span className="font-mono">admin</span> permission on this knowledge base.
              </Callout>
            ) : null}
          </div>
        ) : loading ? (
          <Loading label="Loading access grants…" />
        ) : acls.length === 0 ? (
          <div className="p-4">
            <Callout tone="warning" title="No Access Grants">
              This knowledge base has no access grants — no principal can read it. Add at least one grant below.
            </Callout>
          </div>
        ) : (
          <Table columns={columns} rows={acls} rowKey={aclKey} />
        )}
      </CardBody>

      <AddGrantModal
        kbId={kbId}
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onAdded={() => {
          setAddOpen(false);
          reload();
        }}
      />

      <ConfirmDialog
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => confirmDelete && onDelete(confirmDelete)}
        title="Remove This Access Grant?"
        description="The principal will lose access to this knowledge base."
        confirmLabel="Remove Grant"
        loading={deleting}
      >
        {confirmDelete && (
          <p className="text-sm text-muted">
            The <span className="font-medium text-fg">{humanizeStatus(confirmDelete.principal_type)}</span> grant for{' '}
            <span className="font-mono text-fg">{confirmDelete.principal_id}</span> will be removed. This cannot be
            undone.
          </p>
        )}
      </ConfirmDialog>
    </Card>
  );
}

/** Upsert a grant: re-adding an existing (principal_type, principal_id) replaces its permissions. */
function AddGrantModal({
  kbId,
  open,
  onClose,
  onAdded,
}: {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onAdded: () => void;
}) {
  const toast = useToast();
  const [principalType, setPrincipalType] = useState<RagPrincipalType>('agent');
  const [principalId, setPrincipalId] = useState('');
  const [permissions, setPermissions] = useState<Record<RagPermission, boolean>>({
    read: true,
    query: true,
    ingest: false,
    write: false,
    admin: false,
  });
  const [expiresAt, setExpiresAt] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const selected = PERMISSIONS.filter((p) => permissions[p]);
  const canSubmit = principalId.trim().length > 0 && selected.length > 0;

  function reset() {
    setPrincipalType('agent');
    setPrincipalId('');
    setPermissions({ read: true, query: true, ingest: false, write: false, admin: false });
    setExpiresAt('');
    setError(null);
  }

  function close() {
    if (busy) return;
    reset();
    onClose();
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);

    // datetime-local is local wall-clock; normalize to a UTC ISO instant (blank ⇒ never expires).
    let expiresIso: string | undefined;
    if (expiresAt) {
      const d = new Date(expiresAt);
      if (Number.isNaN(d.getTime())) {
        setError(new Error('The expiry date could not be parsed. Please re-enter it.'));
        setBusy(false);
        return;
      }
      expiresIso = d.toISOString();
    }

    try {
      await addKbAcl(kbId, {
        principal_type: principalType,
        principal_id: principalId.trim(),
        permissions: selected,
        expires_at: expiresIso,
      });
      toast.success('Access grant saved.');
      reset();
      onAdded();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title="Add Access Grant"
      description="Grant a principal permissions on this knowledge base. Re-adding an existing principal replaces its permissions."
      footer={
        <>
          <Button variant="secondary" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button form="add-grant-form" type="submit" loading={busy} disabled={!canSubmit}>
            Save Grant
          </Button>
        </>
      }
    >
      <form id="add-grant-form" onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[180px_1fr]">
          <Select
            label="Principal Type"
            value={principalType}
            onChange={(e) => setPrincipalType(e.target.value as RagPrincipalType)}
          >
            {PRINCIPAL_TYPES.map((t) => (
              <option key={t} value={t}>
                {humanizeStatus(t)}
              </option>
            ))}
          </Select>
          <Input
            label="Principal ID"
            value={principalId}
            onChange={(e) => setPrincipalId(e.target.value)}
            placeholder={principalType === 'tenant' ? '* for all principals in the tenant' : 'Principal identifier'}
            hint={principalType === 'tenant' ? 'Use * to grant every principal in the tenant.' : undefined}
            required
          />
        </div>

        <div>
          <p className="mb-2 text-sm font-medium text-fg">Permissions</p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-3">
            {PERMISSIONS.map((p) => (
              <Switch
                key={p}
                checked={permissions[p]}
                onChange={(v) => setPermissions((prev) => ({ ...prev, [p]: v }))}
                label={humanizeStatus(p)}
              />
            ))}
          </div>
          {selected.length === 0 ? (
            <p className="mt-2 text-xs text-danger">Select at least one permission.</p>
          ) : null}
        </div>

        <Input
          label="Expires At"
          type="datetime-local"
          value={expiresAt}
          onChange={(e) => setExpiresAt(e.target.value)}
          hint="Optional — leave blank for a grant that never expires."
        />

        {error ? <ErrorBanner error={error} title="Could not save the grant" /> : null}
      </form>
    </Modal>
  );
}
