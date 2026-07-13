'use client';

import { useEffect, useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorBanner,
  Input,
  Loading,
  Modal,
  StatusBadge,
  humanizeStatus,
  useToast,
} from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { getMyQuotas, getMyTenant, updateMyTenant } from '@/lib/services';
import { formatTime } from '@/lib/utils';

/**
 * Tenant admin / settings — the caller tenant's identity + editable display name (Auth
 * `GET/PATCH /v1/tenants/me`) plus its effective Contract-19 quota limits, one card per
 * service block (auth / llms / rag / memory / tools). The tenant_id is deliberately NEVER
 * rendered — the tenant NAME is the human handle. Only `name` is safely editable (tenant:admin);
 * a read-only caller 403s on save and the Contract-2 envelope is surfaced in the modal. Quota
 * limits are read-only (`GET /v1/quotas`) and rendered shape-tolerantly so a new service block
 * or limit key shows up with no code change (snake_case keys are humanized for display).
 */
export default function TenantAdminPage() {
  const toast = useToast();
  const tenantQ = useAsync((signal) => getMyTenant(signal), []);
  const quotasQ = useAsync((signal) => getMyQuotas(signal), []);

  const tenant = tenantQ.data;

  const [editOpen, setEditOpen] = useState(false);
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<unknown>(null);

  // Prime the name field from the loaded tenant each time the modal opens.
  useEffect(() => {
    if (editOpen && tenant) {
      setName(tenant.name ?? '');
      setEditError(null);
    }
  }, [editOpen, tenant]);

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setEditError(null);
    try {
      const updated = await updateMyTenant({ name: name.trim() });
      tenantQ.setData(updated);
      toast.success('Tenant updated.');
      setEditOpen(false);
    } catch (err) {
      // A read-only caller (no tenant:admin) 403s here — surface the Contract-2 envelope.
      setEditError(err);
    } finally {
      setSaving(false);
    }
  }

  const quotas = quotasQ.data ?? {};
  const serviceBlocks = Object.entries(quotas).filter(
    ([, v]) => v && typeof v === 'object',
  );

  return (
    <Page>
      <PageHeader
        title="Tenant"
        description="This tenant's identity and effective service quotas (Contract-19)."
      />

      <PageBody>
        <div className="flex flex-col gap-3">
          <Card>
            <CardHeader
              title="Tenant Settings"
              description="Your tenant's identity and plan."
              actions={
                tenant ? (
                  <Button variant="secondary" size="sm" onClick={() => setEditOpen(true)}>
                    Edit
                  </Button>
                ) : undefined
              }
            />
            <CardBody>
              {tenantQ.error ? (
                <ErrorBanner error={tenantQ.error} title="Could not load tenant settings" />
              ) : tenantQ.loading ? (
                <Loading label="Loading tenant…" />
              ) : tenant ? (
                <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Field label="Name" value={tenant.name || '—'} />
                  <Field label="Status" value={<StatusBadge status={tenant.status} />} />
                  <Field label="Plan" value={<span className="capitalize">{tenant.plan || '—'}</span>} />
                  <Field label="Region" value={tenant.region || '—'} />
                  <Field label="Created" value={formatTime(tenant.created_at)} />
                  <Field label="Updated" value={formatTime(tenant.updated_at)} />
                </dl>
              ) : null}
            </CardBody>
          </Card>

          {quotasQ.error ? (
            <ErrorBanner error={quotasQ.error} title="Could not load tenant quotas" />
          ) : quotasQ.loading ? (
            <Loading label="Loading quotas…" />
          ) : serviceBlocks.length === 0 ? (
            <EmptyState title="No quota limits" description="No effective limits were returned for this tenant." />
          ) : (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {serviceBlocks.map(([service, limits]) => (
                <Card key={service}>
                  <CardHeader title={<span className="capitalize">{service} Limits</span>} />
                  <CardBody>
                    <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
                      {Object.entries(limits).map(([key, value]) => (
                        <div key={key} className="contents">
                          <dt className="text-sm text-muted">{humanizeStatus(key)}</dt>
                          <dd className="text-right font-mono text-sm text-fg">
                            {typeof value === 'number' ? value.toLocaleString() : String(value)}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </CardBody>
                </Card>
              ))}
            </div>
          )}
        </div>
      </PageBody>

      <Modal
        open={editOpen}
        onClose={() => {
          if (!saving) setEditOpen(false);
        }}
        title="Edit Tenant"
        description="Update your tenant's display name."
        footer={
          <>
            <Button variant="secondary" onClick={() => setEditOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button form="edit-tenant-form" type="submit" loading={saving} disabled={saving || !name.trim()}>
              Save Changes
            </Button>
          </>
        }
      >
        <form id="edit-tenant-form" onSubmit={onSave} className="flex flex-col gap-4">
          <Input
            label="Display Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Acme Inc."
            hint="Only the display name can be changed here."
          />
          {editError ? <ErrorBanner error={editError} /> : null}
        </form>
      </Modal>
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
