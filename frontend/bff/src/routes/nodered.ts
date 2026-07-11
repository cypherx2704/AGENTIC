/**
 * Embedded Node-RED editor proxy: `/bff/nodered/*` -> the session tenant's Node-RED instance.
 *
 * The SPA iframes `/bff/nodered/` (same-origin). This route:
 *   1. requires a live session (else 401) — no anonymous access to the editor;
 *   2. resolves the tenant's Node-RED routing target from the flow-tool-bridge
 *      (`GET {toolbuilder}/v1/editor-runtime`, authenticated with the session's downstream
 *      JWT), which also ensures the tenant's instance is provisioned;
 *   3. proxies HTTP verbatim AND relays the editor's `comms` WebSocket (live runtime updates),
 *      injecting the Node-RED admin bearer token — the browser never sees the token.
 *
 * Node-RED's httpAdminRoot is configured to `/bff/nodered` so its editor asset URLs (and the
 * `/comms` websocket) resolve unchanged behind this proxy (no HTML/JS rewriting). The
 * per-tenant target is cached briefly so loading the editor's many assets doesn't hammer the
 * bridge.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { WebSocket as WsClient } from 'ws';
import { resolveSession, requireSession } from './sessionHook.js';
import { sanitiseInbound, sanitiseUpstreamResponseHeaders } from '../proxy/headers.js';
import type { LoadedSession } from '../session/types.js';

interface EditorTarget {
  readonly internalHost: string;
  readonly adminToken: string;
}

const TARGET_TTL_MS = 60_000;

export function registerNoderedProxy(app: FastifyInstance): void {
  const { config, sessions, fetch, log, metrics } = app.bff;
  const targetCache = new Map<string, { target: EditorTarget; expires: number }>();

  async function resolveTarget(
    session: LoadedSession,
    req: FastifyRequest,
  ): Promise<EditorTarget | { error: number }> {
    const tenantId = session.data.tenantId;
    const cached = targetCache.get(tenantId);
    if (cached && cached.expires > Date.now()) return cached.target;

    const bridge = config.upstreams['toolbuilder'];
    if (!bridge) return { error: 503 };

    const headers: Record<string, string> = {
      ...sanitiseInbound(req.headers),
      authorization: `Bearer ${session.data.downstreamToken}`,
      'x-tenant-id': tenantId,
    };
    if (req.trace) {
      headers['x-request-id'] = req.trace.requestId;
      headers['traceparent'] = req.trace.traceparent;
    }
    const resp = await fetch(`${bridge}/v1/editor-runtime`, { method: 'GET', headers });
    if (resp.status < 200 || resp.status >= 300) return { error: resp.status === 403 ? 403 : 502 };
    const body = (await resp.json()) as { internal_host?: string; admin_token?: string };
    if (!body.internal_host) return { error: 502 };
    const target: EditorTarget = {
      internalHost: body.internal_host.replace(/\/+$/, ''),
      adminToken: body.admin_token ?? '',
    };
    targetCache.set(tenantId, { target, expires: Date.now() + TARGET_TTL_MS });
    return target;
  }

  // ── HTTP proxy (editor assets + admin API) ───────────────────────────────────
  const handler = async (req: FastifyRequest, reply: FastifyReply): Promise<FastifyReply> => {
    await resolveSession(req, sessions, config.cookie.sessionName);
    const session = requireSession(req, reply);
    if (!session) return reply; // 401 already sent

    const resolved = await resolveTarget(session, req);
    if ('error' in resolved) {
      const map: Record<number, string> = {
        403: 'Editor access requires the tool:admin scope.',
        503: 'Tool Builder backend is not configured.',
        502: 'Could not reach the Tool Builder backend.',
      };
      return reply
        .code(resolved.error)
        .send({ error: { code: resolved.error === 403 ? 'FORBIDDEN' : 'BAD_GATEWAY', message: map[resolved.error] } });
    }

    // req.url already begins with /bff/nodered (== Node-RED httpAdminRoot), so forward it verbatim.
    const targetUrl = `${resolved.internalHost}${req.url}`;
    const method = req.method.toUpperCase();
    const headers: Record<string, string> = {
      ...sanitiseInbound(req.headers),
      authorization: `Bearer ${resolved.adminToken}`,
    };
    if (req.trace) {
      headers['x-request-id'] = req.trace.requestId;
      headers['traceparent'] = req.trace.traceparent;
    }

    const bodyBuf = method === 'GET' || method === 'HEAD' ? undefined : bufferBody(req);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.upstreamTimeoutMs);
    try {
      const upstream = await fetch(targetUrl, { method, headers, body: bodyBuf, signal: controller.signal });
      const respHeaders = sanitiseUpstreamResponseHeaders(
        (n) => upstream.headers.get(n),
        (cb) => upstream.headers.forEach(cb),
      );
      // Binary-safe: Node-RED serves fonts/images/etc.; prefer arrayBuffer (real fetch),
      // fall back to text() for structural fakes.
      const buf = upstream.arrayBuffer
        ? Buffer.from(await upstream.arrayBuffer())
        : Buffer.from(await upstream.text(), 'utf8');
      metrics.proxyRequests.inc({ upstream: 'nodered', outcome: upstream.status < 400 ? 'ok' : 'upstream_error' });
      for (const [k, v] of Object.entries(respHeaders)) {
        if (k === 'content-length') continue;
        reply.header(k, v);
      }
      return reply.code(upstream.status).send(buf);
    } catch (err) {
      metrics.proxyRequests.inc({ upstream: 'nodered', outcome: 'gateway_error' });
      log.warn({ err: (err as Error).message }, 'nodered proxy error');
      return reply.code(502).send({ error: { code: 'BAD_GATEWAY', message: 'Editor request failed' } });
    } finally {
      clearTimeout(timer);
    }
  };

  app.all('/bff/nodered', handler);
  app.all('/bff/nodered/*', handler);

  // ── WebSocket relay for the editor's `/comms` channel ────────────────────────
  // The Node-RED editor opens ws://<origin>/bff/nodered/comms for live runtime events. We
  // relay it to the tenant's Node-RED, injecting the admin token. Registered as a specific
  // static path so it doesn't collide with the /bff/nodered/* HTTP wildcard.
  app.get('/bff/nodered/comms', { websocket: true }, async (conn: unknown, req: FastifyRequest) => {
    // @fastify/websocket v7 passes a SocketStream ({socket}), v8 passes the WebSocket directly.
    const client = (conn as { socket?: unknown }).socket ?? conn;
    const clientWs = client as WsClient;

    await resolveSession(req, sessions, config.cookie.sessionName);
    const session = req.session;
    if (!session) {
      try { clientWs.close(1008, 'unauthorized'); } catch { /* noop */ }
      return;
    }
    const resolved = await resolveTarget(session, req);
    if ('error' in resolved) {
      try { clientWs.close(1011, 'no editor target'); } catch { /* noop */ }
      return;
    }

    const wsBase = resolved.internalHost.replace(/^http/i, 'ws');
    const upstream = new WsClient(`${wsBase}${req.url}`, {
      headers: { authorization: `Bearer ${resolved.adminToken}` },
    });

    const closeBoth = (): void => {
      try { clientWs.close(); } catch { /* noop */ }
      try { upstream.close(); } catch { /* noop */ }
    };

    upstream.on('message', (data: unknown, isBinary: boolean) => {
      if (clientWs.readyState === WsClient.OPEN) clientWs.send(data as Buffer, { binary: isBinary });
    });
    clientWs.on('message', (data: unknown, isBinary: boolean) => {
      if (upstream.readyState === WsClient.OPEN) upstream.send(data as Buffer, { binary: isBinary });
    });
    upstream.on('close', closeBoth);
    upstream.on('error', (err: Error) => { log.warn({ err: err.message }, 'nodered comms upstream error'); closeBoth(); });
    clientWs.on('close', closeBoth);
    clientWs.on('error', closeBoth);
  });
}

function bufferBody(req: FastifyRequest): Buffer | undefined {
  const body = req.body as unknown;
  if (body === undefined || body === null) return undefined;
  if (Buffer.isBuffer(body)) return body;
  if (typeof body === 'string') return Buffer.from(body, 'utf8');
  return Buffer.from(JSON.stringify(body), 'utf8');
}
