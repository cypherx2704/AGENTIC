/**
 * Downstream proxy: `/bff/api/<service>/<rest...>` -> the configured platform service
 * (WP13 §5/§6). This is the heart of the security boundary:
 *
 *   - requires a valid session (else 401) — no anonymous proxying
 *   - resolves the upstream from the first path segment (auth/llms/guardrails/xagent/rag)
 *   - strips hop-by-hop + client identity headers (proxy/headers.ts)
 *   - injects Authorization (the session's downstream token), X-Tenant-ID, X-Request-ID,
 *     traceparent — the browser never sees or controls the token
 *   - serves expensive dashboard GETs from a 30s per-tenant cache
 *   - relays Server-Sent-Events for the xagent task stream without buffering
 *
 * The raw downstream token NEVER appears in a response body or header sent to the
 * browser; only opaque upstream payloads (already tenant-scoped by the platform's RLS)
 * flow back.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { resolveSession, requireSession } from '../routes/sessionHook.js';
import {
  buildUpstreamHeaders,
  sanitiseInbound,
  sanitiseUpstreamResponseHeaders,
} from './headers.js';
import { isCacheablePath } from './cache.js';

/** Split `/bff/api/<service>/<rest>` -> { service, rest } or null if malformed. */
export function parseProxyPath(url: string): { service: string; rest: string } | null {
  const pathOnly = url.split('?')[0] ?? '';
  const prefix = '/bff/api/';
  if (!pathOnly.startsWith(prefix)) return null;
  const remainder = pathOnly.slice(prefix.length);
  const slash = remainder.indexOf('/');
  if (slash === -1) {
    return remainder.length > 0 ? { service: remainder, rest: '' } : null;
  }
  return { service: remainder.slice(0, slash), rest: remainder.slice(slash) };
}

/** Detect the xagent SSE stream route so we can switch to a streaming relay. */
function isStreamRoute(service: string, rest: string): boolean {
  return service === 'xagent' && /^\/v1\/tasks\/[^/]+\/stream\/?$/.test(rest);
}

export function registerProxy(app: FastifyInstance): void {
  const { config, sessions, metrics, cache, fetch, log } = app.bff;

  app.all('/bff/api/*', async (req: FastifyRequest, reply: FastifyReply) => {
    // 1. Authn — every proxied call requires a live session.
    await resolveSession(req, sessions, config.cookie.sessionName);
    const session = requireSession(req, reply);
    if (!session) return reply; // requireSession already sent 401

    // 2. Resolve upstream.
    const parsed = parseProxyPath(req.url);
    if (!parsed) {
      return reply.code(404).send({ error: { code: 'NOT_FOUND', message: 'Unknown proxy path' } });
    }
    const base = config.upstreams[parsed.service];
    if (!base) {
      metrics.proxyRequests.inc({ upstream: parsed.service, outcome: 'unconfigured' });
      return reply
        .code(404)
        .send({ error: { code: 'NOT_FOUND', message: `Unknown upstream: ${parsed.service}` } });
    }

    const query = req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '';
    const targetUrl = `${base}${parsed.rest}${query}`;
    const method = req.method.toUpperCase();
    const tenantId = session.data.tenantId;

    // 3. Dashboard cache (GET only, expensive paths only).
    const cacheKey = `${method} ${parsed.service}${parsed.rest}${query}`;
    const cacheable = method === 'GET' && isCacheablePath(parsed.rest);
    if (cacheable) {
      const hit = cache.get(tenantId, cacheKey);
      if (hit) {
        metrics.cacheHits.inc({ result: 'hit' });
        applyHeaders(reply, hit.headers);
        reply.header('x-bff-cache', 'hit');
        return reply.code(hit.status).send(hit.body);
      }
      metrics.cacheHits.inc({ result: 'miss' });
    }

    // 4. Build upstream headers (sanitise inbound + inject trusted identity).
    const headers = buildUpstreamHeaders(sanitiseInbound(req.headers), {
      downstreamToken: session.data.downstreamToken,
      tenantId,
      trace: req.trace,
    });

    // 5. SSE stream relay (xagent task stream).
    if (isStreamRoute(parsed.service, parsed.rest)) {
      return relayStream(app, reply, targetUrl, headers, parsed.service);
    }

    // 6. Buffered proxy for everything else.
    const bodyBuf = bufferBody(req);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.upstreamTimeoutMs);
    try {
      const upstream = await fetch(targetUrl, {
        method,
        headers,
        body: method === 'GET' || method === 'HEAD' ? undefined : bodyBuf,
        signal: controller.signal,
      });
      const respHeaders = sanitiseUpstreamResponseHeaders(
        (n) => upstream.headers.get(n),
        (cb) => upstream.headers.forEach(cb),
      );
      const text = await upstream.text();
      const buf = Buffer.from(text, 'utf8');

      if (cacheable && upstream.status >= 200 && upstream.status < 300) {
        cache.set(tenantId, cacheKey, { status: upstream.status, headers: respHeaders, body: buf });
      }
      metrics.proxyRequests.inc({
        upstream: parsed.service,
        outcome: upstream.status < 400 ? 'ok' : 'upstream_error',
      });

      applyHeaders(reply, respHeaders);
      return reply.code(upstream.status).send(buf);
    } catch (err) {
      metrics.proxyRequests.inc({ upstream: parsed.service, outcome: 'gateway_error' });
      log.warn({ err: (err as Error).message, upstream: parsed.service }, 'proxy upstream error');
      return reply
        .code(502)
        .send({ error: { code: 'BAD_GATEWAY', message: 'Upstream request failed' } });
    } finally {
      clearTimeout(timer);
    }
  });
}

function applyHeaders(reply: FastifyReply, headers: Readonly<Record<string, string>>): void {
  for (const [k, v] of Object.entries(headers)) {
    // never re-emit a hop-by-hop / set-cookie (already filtered) — content-length is
    // recomputed by Fastify on send.
    if (k === 'content-length') continue;
    reply.header(k, v);
  }
}

function bufferBody(req: FastifyRequest): Buffer | undefined {
  const body = req.body as unknown;
  if (body === undefined || body === null) return undefined;
  if (Buffer.isBuffer(body)) return body;
  if (typeof body === 'string') return Buffer.from(body, 'utf8');
  return Buffer.from(JSON.stringify(body), 'utf8');
}

/**
 * SSE relay: open the upstream stream and pipe its chunks straight to the client with
 * `text/event-stream`, no buffering. The downstream token is injected on the upstream
 * request only — the client just sees the events.
 */
async function relayStream(
  app: FastifyInstance,
  reply: FastifyReply,
  targetUrl: string,
  headers: Record<string, string>,
  service: string,
): Promise<FastifyReply> {
  const { fetch, metrics, log } = app.bff;
  try {
    const upstream = await fetch(targetUrl, {
      method: 'GET',
      headers: { ...headers, accept: 'text/event-stream' },
    });
    reply.raw.setHeader('Content-Type', 'text/event-stream');
    reply.raw.setHeader('Cache-Control', 'no-cache, no-transform');
    reply.raw.setHeader('Connection', 'keep-alive');
    reply.raw.setHeader('X-Accel-Buffering', 'no');
    reply.raw.statusCode = upstream.status;

    const body = upstream.body as
      | AsyncIterable<Uint8Array>
      | { getReader?: () => { read(): Promise<{ done: boolean; value?: Uint8Array }> } }
      | undefined;

    if (body && typeof (body as AsyncIterable<Uint8Array>)[Symbol.asyncIterator] === 'function') {
      for await (const chunk of body as AsyncIterable<Uint8Array>) {
        reply.raw.write(Buffer.from(chunk));
      }
    } else if (body && typeof (body as { getReader?: unknown }).getReader === 'function') {
      const reader = (body as { getReader: () => { read(): Promise<{ done: boolean; value?: Uint8Array }> } }).getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) reply.raw.write(Buffer.from(value));
      }
    } else {
      // Upstream returned a buffered body (e.g. fake fetch in tests) — flush it whole.
      const text = await upstream.text();
      reply.raw.write(text);
    }
    metrics.proxyRequests.inc({ upstream: service, outcome: 'stream' });
    reply.raw.end();
  } catch (err) {
    log.warn({ err: (err as Error).message }, 'sse relay error');
    if (!reply.raw.headersSent) {
      reply.raw.statusCode = 502;
    }
    reply.raw.end();
  }
  return reply;
}
