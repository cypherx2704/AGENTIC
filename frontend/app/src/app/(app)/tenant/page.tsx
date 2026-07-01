'use client';

import { PageHeader } from '@/components/AppShell';
import { useSession } from '@/components/SessionProvider';
import { Badge, Card, CardBody, CardHeader, ErrorBanner, EmptyState, Loading } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { getMyQuotas } from '@/lib/services';

/**
 * Tenant admin / settings — the caller tenant's identity (from the session) plus its
 * effective Contract-19 quota limits, one card per service block (auth / llms / rag /
 * memory / tools). Read-only: limits come from `GET /v1/quotas` (self-service, needs
 * tenant:read|tenant:admin). Rendered shape-tolerantly so a new service block or limit
 * key shows up with no code change. Platform-admin quota OVERRIDES are set out-of-band
 * (`PUT /v1/admin/tenants/{id}/quotas`); this screen surfaces the resolved effective view.
 */
export default function TenantAdminPage() {
  const { session } = useSession();
  const quotasQ = useAsync((signal) => getMyQuotas(signal), []);

  const quotas = quotasQ.data ?? {};
  const serviceBlocks = Object.entries(quotas).filter(
    ([, v]) => v && typeof v === 'object',
  );

  return (
    <div>
      <PageHeader
        title="Tenant"
        description="This tenant's identity and effective service quotas (Contract-19)."
      />

      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader title="Identity" description="From your authenticated session." />
          <CardBody>
            <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field
                label="Tenant ID"
                value={<span className="font-mono text-xs">{session?.tenant_id ?? '—'}</span>}
              />
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted">Session scopes</p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {(session?.scopes ?? []).map((s) => (
                    <Badge key={s} tone="info">
                      {s}
                    </Badge>
                  ))}
                  {(session?.scopes?.length ?? 0) === 0 && <span className="text-sm text-muted">none</span>}
                </div>
              </div>
            </dl>
          </CardBody>
        </Card>

        {quotasQ.error ? (
          <ErrorBanner error={quotasQ.error} title="Could not load tenant quotas" />
        ) : quotasQ.loading ? (
          <Loading label="Loading quotas…" />
        ) : serviceBlocks.length === 0 ? (
          <EmptyState title="No quota limits" description="No effective limits were returned for this tenant." />
        ) : (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {serviceBlocks.map(([service, limits]) => (
              <Card key={service}>
                <CardHeader title={`${service} limits`} />
                <CardBody>
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
                    {Object.entries(limits).map(([key, value]) => (
                      <div key={key} className="contents">
                        <dt className="text-sm text-muted">{key}</dt>
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
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
      <p className="mt-1 text-sm text-fg">{value}</p>
    </div>
  );
}
