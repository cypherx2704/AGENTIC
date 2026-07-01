/**
 * Operational endpoints (WP13 §6):
 *   GET /livez   — process liveness (always 200 if the event loop runs)
 *   GET /readyz  — readiness: can we reach Valkey? (the BFF is useless without sessions)
 *   GET /metrics — Prometheus text exposition
 *
 * These are unauthenticated by design (scraped by the platform / k8s), and carry no
 * tenant data.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

export interface ReadinessProbe {
  /** Resolve true when the session store backend is reachable. */
  ping(): Promise<boolean>;
}

export function registerHealthRoutes(app: FastifyInstance, probe: ReadinessProbe): void {
  const { metrics } = app.bff;

  app.get('/livez', async (_req: FastifyRequest, reply: FastifyReply) => {
    return reply.code(200).send({ status: 'ok' });
  });

  app.get('/readyz', async (_req: FastifyRequest, reply: FastifyReply) => {
    let valkey = false;
    try {
      valkey = await probe.ping();
    } catch {
      valkey = false;
    }
    if (valkey) {
      return reply.code(200).send({ status: 'ready', checks: { valkey: 'ok' } });
    }
    return reply.code(503).send({ status: 'unready', checks: { valkey: 'down' } });
  });

  app.get('/metrics', async (_req: FastifyRequest, reply: FastifyReply) => {
    reply.header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8');
    return reply.code(200).send(metrics.registry.render());
  });

  // GET /bff/health — operational aggregate of each configured upstream's /livez + /readyz.
  // OPS-ONLY: no tenant data, no app logic, no aggregation of business state (keeps the BFF a thin
  // trust boundary, not a "God service"). Consumed by the SPA Platform Health screen, which expects
  //   { services: { <name>: { livez: <status|null>, readyz: <status|null> } } }.
  app.get('/bff/health', async (_req: FastifyRequest, reply: FastifyReply) => {
    const { config, fetch } = app.bff;
    const probe = async (base: string, path: string): Promise<number | null> => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), Math.min(config.upstreamTimeoutMs, 5000));
      try {
        const res = await fetch(`${base}${path}`, { method: 'GET', signal: ctrl.signal });
        return res.status;
      } catch {
        return null; // unreachable / timed out
      } finally {
        clearTimeout(timer);
      }
    };
    const entries = await Promise.all(
      Object.entries(config.upstreams).map(async ([name, base]) => {
        const [livez, readyz] = await Promise.all([probe(base, '/livez'), probe(base, '/readyz')]);
        return [name, { livez, readyz }] as const;
      }),
    );
    return reply.code(200).send({ services: Object.fromEntries(entries) });
  });
}
