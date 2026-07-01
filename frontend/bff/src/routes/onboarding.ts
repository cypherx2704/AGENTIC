/**
 * Self-serve onboarding routes (WP04 Contract-20) — the SPA's public, pre-account funnel.
 *
 *   POST /bff/onboarding/signup   { email, tenant_name, captcha_token } -> 202 (queues verify email)
 *   GET  /bff/onboarding/verify   ?token=...  -> 200 { tenant_id, agent_id, api_key, ... } | 410 Gone
 *   POST /bff/onboarding/resend   { email }   -> 202 (anti-enumeration)
 *
 * These are the ONLY un-authenticated mutating routes on the BFF. They exist because the SPA
 * talks ONLY to the BFF (never to Auth directly), yet a brand-new user has no session/JWT/CSRF
 * token yet. So unlike the /bff/api/* proxy they:
 *   - require NO session and inject NO identity headers (pre-account — there is no caller),
 *   - are CSRF-exempt for the two POSTs (see EXEMPT_PATHS in security/csrf.ts) — there cannot be
 *     a prior CSRF token, exactly like /bff/login,
 *   - forward verbatim to Auth's permit-all /v1/onboarding/* endpoints, passing the upstream
 *     status + Contract-2 body straight through (Auth's signup/resend bodies are deliberately
 *     anti-enumeration and verify's body is the intended one-time payload — nothing to sanitise).
 *
 * The raw initial api_key is returned ONCE in the verify response; security/headers.ts marks
 * /bff/onboarding responses no-store so it is never cached.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

export function registerOnboardingRoutes(app: FastifyInstance): void {
  const { config, fetch, log } = app.bff;
  const authBase = config.upstreams.auth;

  /**
   * Forward a request to an Auth onboarding endpoint and relay status + body verbatim. No
   * identity headers are sent (pre-account); only correlation ids ride along. A transport
   * failure becomes a Contract-2 502 — the SPA's ErrorBanner renders it like any other error.
   */
  async function forward(
    req: FastifyRequest,
    reply: FastifyReply,
    method: 'GET' | 'POST',
    upstreamPath: string,
    opts: { body?: unknown; query?: Record<string, string | undefined>; timeoutMs?: number } = {},
  ): Promise<void> {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(opts.query ?? {})) {
      if (typeof v === 'string' && v !== '') params.set(k, v);
    }
    const qs = params.toString();
    const url = `${authBase}${upstreamPath}${qs ? `?${qs}` : ''}`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? config.upstreamTimeoutMs);
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: {
          'content-type': 'application/json',
          accept: 'application/json',
          'x-request-id': req.trace.requestId,
          traceparent: req.trace.traceparent,
        },
        body: method === 'POST' ? JSON.stringify(opts.body ?? {}) : undefined,
        signal: controller.signal,
      });
    } catch (err) {
      log.error(
        { err: (err as Error).message, reqId: req.trace.requestId, path: upstreamPath },
        'onboarding: auth upstream unreachable',
      );
      void reply
        .code(502)
        .send({ error: { code: 'AUTH_UPSTREAM_ERROR', message: 'Onboarding service unavailable' } });
      return;
    } finally {
      clearTimeout(timer);
    }

    const text = await res.text();
    reply.header('content-type', res.headers.get('content-type') ?? 'application/json');
    void reply.code(res.status).send(text);
  }

  // ── POST /bff/onboarding/signup ─────────────────────────────────────────────────
  app.post('/bff/onboarding/signup', async (req: FastifyRequest, reply: FastifyReply) => {
    await forward(req, reply, 'POST', '/v1/onboarding/signup', { body: req.body });
  });

  // ── GET /bff/onboarding/verify?token=... ────────────────────────────────────────
  app.get('/bff/onboarding/verify', async (req: FastifyRequest, reply: FastifyReply) => {
    const token = (req.query as Record<string, unknown> | undefined)?.token;
    // Verify provisions server-side under a possibly-slow DB; give it a longer budget than the normal
    // proxy timeout so the BFF never abandons a call Auth will still complete (see config).
    await forward(req, reply, 'GET', '/v1/onboarding/verify', {
      query: { token: typeof token === 'string' ? token : undefined },
      timeoutMs: config.onboardingVerifyTimeoutMs,
    });
  });

  // ── POST /bff/onboarding/resend ─────────────────────────────────────────────────
  app.post('/bff/onboarding/resend', async (req: FastifyRequest, reply: FastifyReply) => {
    await forward(req, reply, 'POST', '/v1/onboarding/resend', { body: req.body });
  });
}
