/**
 * Application factory. Builds a fully-wired Fastify instance from an injected
 * dependency bundle so production and tests share one assembly path:
 *
 *   - production: real ioredis + the global fetch (server.ts)
 *   - tests:      ioredis-mock + a fake fetch, no live infra
 *
 * Wiring order matters and is deliberate:
 *   1. cookie parsing                (so hooks can read cookies)
 *   2. trace context (onRequest)     (request id + traceparent for everything)
 *   3. security headers (onSend)     (applied to every response, incl. errors)
 *   4. request counter (onResponse)
 *   5. CSRF guard (preHandler)       (after the session is resolved for /bff/api)
 *   6. routes: auth, proxy, health
 */
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';
import cookie from '@fastify/cookie';
import websocket from '@fastify/websocket';
import type { Config } from './config/index.js';
import type { BffContext, FetchLike } from './context.js';
import { SessionCrypto } from './session/crypto.js';
import { SessionStore, type RedisLike } from './session/store.js';
import { createMetrics } from './observability/metrics.js';
import { createLogger, type Logger } from './observability/logger.js';
import { AuthClient } from './upstream/authClient.js';
import { DashboardCache } from './proxy/cache.js';
import { deriveTraceContext } from './security/trace.js';
import { registerSecurityHeaders } from './security/headers.js';
import { makeCsrfGuard } from './security/csrf.js';
import { registerAuthRoutes } from './routes/auth.js';
import { registerOnboardingRoutes } from './routes/onboarding.js';
import { registerHealthRoutes, type ReadinessProbe } from './routes/health.js';
import { registerProxy } from './proxy/index.js';
import { registerNoderedProxy } from './routes/nodered.js';
import { resolveSession } from './routes/sessionHook.js';

export interface BuildAppDeps {
  readonly config: Config;
  readonly redis: RedisLike;
  readonly fetch: FetchLike;
  readonly readiness: ReadinessProbe;
  readonly logger?: Logger;
}

export async function buildApp(deps: BuildAppDeps): Promise<FastifyInstance> {
  const { config, redis, fetch, readiness } = deps;
  const log = deps.logger ?? createLogger(config.logLevel);

  const crypto = new SessionCrypto(config.sessionKek);
  const sessions = new SessionStore(redis, crypto, {
    keyPrefix: config.sessionKeyPrefix,
    ttlSeconds: config.sessionTtlSeconds,
  });
  const metrics = createMetrics();
  const authClient = new AuthClient(config.upstreams.auth as string, fetch, config.upstreamTimeoutMs);
  const cache = new DashboardCache(config.dashboardCacheTtlSeconds);

  const context: BffContext = { config, sessions, metrics, log, authClient, cache, fetch };

  const app = Fastify({
    logger: false, // we manage our own structured logger
    disableRequestLogging: true,
    trustProxy: true,
    bodyLimit: 1_048_576, // 1 MiB — the BFF forwards JSON, not uploads
  });

  app.decorate('bff', context);

  // 1. Cookies.
  await app.register(cookie, {});

  // WebSocket support (registered before routes) — powers the embedded Node-RED editor's
  // live "comms" channel so the drag-and-drop canvas gets real-time runtime updates.
  await app.register(websocket, { options: { maxPayload: 8 * 1024 * 1024 } });

  // Capture raw body for the proxy (so we can forward bodies for arbitrary content
  // types) while still parsing JSON for our own routes. We add a permissive parser
  // that keeps the Buffer for non-JSON content types.
  app.addContentTypeParser(
    ['text/*', 'application/octet-stream', 'application/x-www-form-urlencoded'],
    { parseAs: 'buffer' },
    (_req, body, done) => done(null, body),
  );

  // 2. Trace context — first thing on every request.
  app.addHook('onRequest', async (req: FastifyRequest) => {
    req.trace = deriveTraceContext(
      headerValue(req.headers['x-request-id']),
      headerValue(req.headers['traceparent']),
    );
    req.headers['x-request-id'] = req.trace.requestId;
  });

  // Echo correlation ids on the response.
  app.addHook('onSend', async (req: FastifyRequest, reply: FastifyReply, payload: unknown) => {
    if (req.trace) {
      reply.header('x-request-id', req.trace.requestId);
      reply.header('traceparent', req.trace.traceparent);
    }
    return payload;
  });

  // 3. Security headers on every response.
  registerSecurityHeaders(app, config);

  // 4. Request counter.
  app.addHook('onResponse', async (req: FastifyRequest, reply: FastifyReply) => {
    const cls = `${Math.floor(reply.statusCode / 100)}xx`;
    metrics.httpRequests.inc({ method: req.method, status: cls });
  });

  // 5. CSRF guard — resolve the session first (so the guard can compare the bound
  //    token), then enforce double-submit on mutating requests.
  const csrfGuard = makeCsrfGuard({
    headerName: config.csrf.headerName,
    cookieName: config.cookie.csrfName,
    onViolation: (reason) => metrics.csrfViolations.inc({ reason }),
  });
  app.addHook('preHandler', async (req: FastifyRequest, reply: FastifyReply) => {
    const method = req.method.toUpperCase();
    if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return;
    // The embedded Node-RED editor (iframe under /bff/nodered/*) cannot carry our CSRF
    // token; SameSite=lax cookies + the required session (enforced in the proxy route) are
    // the CSRF controls there, so skip the double-submit guard for that prefix only.
    if (req.url.startsWith('/bff/nodered')) {
      await resolveSession(req, sessions, config.cookie.sessionName);
      return;
    }
    // Resolve session so CSRF can validate the bound token (idempotent; proxy re-uses it).
    await resolveSession(req, sessions, config.cookie.sessionName);
    await csrfGuard(req, reply);
  });

  // 6. Routes.
  registerAuthRoutes(app);
  // Public, pre-account onboarding funnel (Contract-20). Mounted under /bff/onboarding/* so it
  // does NOT collide with the /bff/api/* proxy wildcard; signup/resend are CSRF-exempt (no session yet).
  registerOnboardingRoutes(app);
  registerProxy(app);
  registerNoderedProxy(app);
  registerHealthRoutes(app, readiness);

  // Uniform JSON 404 + error envelope.
  app.setNotFoundHandler((_req, reply) => {
    void reply.code(404).send({ error: { code: 'NOT_FOUND', message: 'Not found' } });
  });
  app.setErrorHandler((err, req, reply) => {
    const status = (err.statusCode && err.statusCode >= 400 ? err.statusCode : 500) as number;
    log.error({ err: err.message, reqId: req.trace?.requestId, status }, 'request error');
    void reply
      .code(status)
      .send({ error: { code: status >= 500 ? 'INTERNAL' : 'REQUEST_ERROR', message: status >= 500 ? 'Internal error' : err.message } });
  });

  return app;
}

function headerValue(v: string | string[] | undefined): string | undefined {
  if (Array.isArray(v)) return v[0];
  return v;
}
