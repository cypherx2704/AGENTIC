'use client';

import { useEffect, useState } from 'react';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import { Badge, Card, CardBody, CardHeader, ErrorBanner, Loading } from '@/components/ui';
import { bffFetch } from '@/lib/bff-client';
import { config } from '@/lib/config';

interface ServiceHealth {
  name: string;
  livez: number | null;
  readyz: number | null;
}

/**
 * Probe the BFF health aggregate. We try a couple of likely shapes/paths since the BFF
 * health surface contract is owned by the sibling agent:
 *   - GET /bff/health  ->  { services: { auth: {livez, readyz}, ... } }  (preferred)
 *   - GET /bff/health  ->  { auth: 200, llms: 200, ... }                  (flat status map)
 */
async function probeHealth(signal: AbortSignal): Promise<ServiceHealth[]> {
  const raw = await bffFetch<any>('/health', { signal });
  const services = (raw && typeof raw === 'object' && 'services' in raw ? raw.services : raw) ?? {};
  const out: ServiceHealth[] = [];
  for (const [name, value] of Object.entries(services)) {
    if (value && typeof value === 'object') {
      const v = value as Record<string, unknown>;
      out.push({
        name,
        livez: typeof v.livez === 'number' ? v.livez : null,
        readyz: typeof v.readyz === 'number' ? v.readyz : null,
      });
    } else if (typeof value === 'number') {
      out.push({ name, livez: null, readyz: value });
    } else {
      out.push({ name, livez: null, readyz: null });
    }
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

function probeBadge(status: number | null) {
  if (status === null) return <Badge>—</Badge>;
  if (status >= 200 && status < 300) return <Badge tone="success">{status} OK</Badge>;
  if (status >= 500) return <Badge tone="danger">{status}</Badge>;
  return <Badge tone="warning">{status}</Badge>;
}

export default function HealthPage() {
  const [services, setServices] = useState<ServiceHealth[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function load(spinner: boolean) {
      if (spinner) setLoading(true);
      try {
        const result = await probeHealth(controller.signal);
        setServices(result);
        setError(null);
        setLastChecked(new Date());
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) setError(err);
      } finally {
        if (spinner) setLoading(false);
      }
    }

    void load(true);
    const timer = setInterval(() => void load(false), config.healthPollMs);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, []);

  return (
    <Page>
      <PageHeader
        title="Platform Health"
        description={`livez / readyz of each service via the BFF (auto-refresh ${config.healthPollMs / 1000}s).`}
        actions={lastChecked ? <span className="text-xs text-muted">Checked {lastChecked.toLocaleTimeString()}</span> : null}
      />
      <PageBody>

      {error ? (
        <ErrorBanner
          error={error}
          title="Could not reach the platform health aggregate"
          className="mb-4"
        />
      ) : null}

      {loading ? (
        <Loading label="Probing services…" />
      ) : services.length === 0 && !error ? (
        <Card>
          <CardBody>
            <p className="text-sm text-muted">The BFF returned no services. Check that the health aggregate is wired.</p>
          </CardBody>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {services.map((s) => {
            const healthy = s.readyz != null && s.readyz >= 200 && s.readyz < 300;
            return (
              <Card key={s.name} className={healthy ? '' : 'border-danger/40'}>
                <CardHeader title={<span className="capitalize">{s.name}</span>} />
                <CardBody>
                  <div className="flex flex-col gap-3">
                    <div className="flex items-center justify-between">
                      <span className="text-sm text-muted">Liveness</span>
                      {probeBadge(s.livez)}
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-sm text-muted">Readiness</span>
                      {probeBadge(s.readyz)}
                    </div>
                  </div>
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}
      </PageBody>
    </Page>
  );
}
