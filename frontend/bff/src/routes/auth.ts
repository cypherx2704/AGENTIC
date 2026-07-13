/**
 * Auth/session routes — the SPA's session-bootstrap contract.
 *
 *   POST /bff/login    { email, password }
 *       Email/password login: Auth verifies the password and mints the tenant ORCHESTRATOR's
 *       agent JWT; the BFF stores it in an encrypted Valkey session and sets the httpOnly session
 *       cookie + the CSRF cookie. Returns { tenant_id, scopes, csrf_token } — never the token.
 *
 *   POST /bff/register { email, password, tenant_name?, full_name? }
 *       Self-serve signup: provisions tenant + user + orchestrator + initial api_key. Returns the
 *       orchestrator's one-time api_key (the ONLY time it is exposed) so the user can use the SDK.
 *
 *   GET  /bff/auth/google           302 → Google consent screen.
 *   GET  /bff/auth/google/callback  Google redirects the browser here with ?code&state; the BFF
 *       exchanges the code server-side, opens a session, then 302s to the SPA. (Token stays here.)
 *
 *   POST /bff/logout   destroys the session + clears both cookies.
 *   GET  /bff/me       session bootstrap: { authenticated, tenant_id, scopes, csrf_token }.
 *
 * The downstream agent JWT lives ONLY in the encrypted session — never returned to the browser.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { AuthExchangeError } from '../upstream/authClient.js';
import type { UserSessionResult } from '../upstream/authClient.js';
import { generateCsrfToken } from '../session/store.js';
import { resolveSession } from './sessionHook.js';
import { clearAuthCookies, setCsrfCookie, setSessionCookie } from './cookies.js';
import type { SessionData } from '../session/types.js';

interface LoginBody {
  email?: unknown;
  password?: unknown;
}

interface RegisterBody {
  email?: unknown;
  password?: unknown;
  tenant_name?: unknown;
  full_name?: unknown;
}

function asNonEmptyString(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() !== '' ? v.trim() : undefined;
}

export function registerAuthRoutes(app: FastifyInstance): void {
  const { config, sessions, authClient, metrics, log } = app.bff;

  /** Build + persist a session from an Auth user-session result; sets cookies. Returns csrf token. */
  async function openSession(reply: FastifyReply, result: UserSessionResult): Promise<string> {
    const csrfToken = generateCsrfToken();
    const nowSeconds = Math.floor(Date.now() / 1000);
    const hasRefresh = Boolean(result.refreshToken);
    // The whole session lives at most this long; without a refresh token (legacy api_key login) it
    // is still bounded by the sliding idle TTL.
    const sessionLifetimeSeconds =
      hasRefresh && result.refreshExpiresIn > 0 ? result.refreshExpiresIn : config.sessionTtlSeconds;
    const data: SessionData = {
      tenantId: result.tenantId,
      agentId: result.agentId,
      userId: result.userId || undefined,
      scopes: result.scopes,
      downstreamToken: result.token,
      tokenExpiresAt: nowSeconds + result.expiresIn,
      refreshToken: hasRefresh ? result.refreshToken : undefined,
      refreshExpiresAt: hasRefresh ? nowSeconds + sessionLifetimeSeconds : undefined,
      csrfToken,
      createdAt: Date.now(),
    };
    const sid = await sessions.create(data);
    // Cookie Max-Age spans the whole session lifetime, not just the <=1h access token — otherwise the
    // browser cookie would expire ~1h in while the (silently-refreshed) session is still alive.
    setSessionCookie(reply, config, sid, sessionLifetimeSeconds);
    setCsrfCookie(reply, config, csrfToken, sessionLifetimeSeconds);
    return csrfToken;
  }

  // ── POST /bff/login (email + password) ───────────────────────────────────────────
  app.post('/bff/login', async (req: FastifyRequest, reply: FastifyReply) => {
    const body = (req.body ?? {}) as LoginBody;
    const email = asNonEmptyString(body.email);
    const password = asNonEmptyString(body.password);

    if (!email || !password) {
      metrics.loginAttempts.inc({ outcome: 'bad_request' });
      return reply.code(400).send({
        error: { code: 'INVALID_REQUEST', message: 'email and password are required' },
      });
    }

    let result: UserSessionResult;
    try {
      result = await authClient.loginWithPassword(email, password, req.trace);
    } catch (err) {
      return handleAuthError(err, reply);
    }

    const csrfToken = await openSession(reply, result);
    metrics.loginAttempts.inc({ outcome: 'success' });
    return reply.code(200).send({
      authenticated: true,
      tenant_id: result.tenantId,
      scopes: result.scopes,
      csrf_token: csrfToken,
    });
  });

  // ── POST /bff/register ─────────────────────────────────────────────────────────────
  app.post('/bff/register', async (req: FastifyRequest, reply: FastifyReply) => {
    const body = (req.body ?? {}) as RegisterBody;
    const email = asNonEmptyString(body.email);
    const password = asNonEmptyString(body.password);
    const tenantName = asNonEmptyString(body.tenant_name);
    const fullName = asNonEmptyString(body.full_name);

    if (!email || !password) {
      return reply.code(400).send({
        error: { code: 'INVALID_REQUEST', message: 'email and password are required' },
      });
    }

    let reg;
    try {
      reg = await authClient.register(
        { email, password, tenant_name: tenantName, full_name: fullName },
        req.trace,
      );
    } catch (err) {
      return handleAuthError(err, reply);
    }

    // Auto-login the new user so the SPA lands authenticated.
    let session: UserSessionResult | undefined;
    try {
      session = await authClient.loginWithPassword(email, password, req.trace);
    } catch (err) {
      log.warn({ err: (err as Error).message }, 'register: auto-login after signup failed');
    }

    const csrfToken = session ? await openSession(reply, session) : undefined;
    // The orchestrator's initial api_key is shown ONCE — surface it so the user can save it.
    return reply.code(201).send({
      authenticated: Boolean(session),
      tenant_id: reg.tenantId,
      orchestrator_agent_id: reg.orchestratorAgentId,
      api_key: reg.apiKey,
      key_prefix: reg.keyPrefix,
      scopes: session?.scopes ?? [],
      csrf_token: csrfToken,
    });
  });

  // ── GET /bff/auth/google ─────────────────────────────────────────────────────────
  app.get('/bff/auth/google', async (req: FastifyRequest, reply: FastifyReply) => {
    try {
      const url = await authClient.googleAuthUrl(req.trace);
      return reply.redirect(302, url);
    } catch (err) {
      if (err instanceof AuthExchangeError && err.status === 501) {
        return reply.code(501).send({
          error: { code: 'GOOGLE_OAUTH_NOT_CONFIGURED', message: 'Google sign-in is not configured' },
        });
      }
      log.error({ err: (err as Error).message }, 'google start failed');
      return reply.code(502).send({ error: { code: 'AUTH_UPSTREAM_ERROR', message: 'Google sign-in unavailable' } });
    }
  });

  // ── GET /bff/auth/google/callback ──────────────────────────────────────────────────
  app.get('/bff/auth/google/callback', async (req: FastifyRequest, reply: FastifyReply) => {
    const q = (req.query ?? {}) as Record<string, unknown>;
    const code = asNonEmptyString(q.code);
    const state = asNonEmptyString(q.state);
    if (!code || !state) {
      return reply.code(400).send({ error: { code: 'INVALID_REQUEST', message: 'missing code/state' } });
    }
    let result: UserSessionResult;
    try {
      result = await authClient.exchangeGoogleCode(code, state, req.trace);
    } catch (err) {
      log.warn({ err: (err as Error).message }, 'google callback exchange failed');
      // Bounce back to the SPA login with an error marker rather than rendering JSON in the browser.
      return reply.redirect(302, `${config.postLoginRedirect}login?error=google`);
    }
    await openSession(reply, result);
    metrics.loginAttempts.inc({ outcome: 'success' });
    return reply.redirect(302, config.postLoginRedirect);
  });

  // ── POST /bff/logout ───────────────────────────────────────────────────────────
  app.post('/bff/logout', async (req: FastifyRequest, reply: FastifyReply) => {
    await resolveSession(req, sessions, config.cookie.sessionName);
    if (req.session) {
      // Revoke the refresh token upstream so the session can't be renewed after logout (best-effort).
      const refreshToken = req.session.data.refreshToken;
      if (refreshToken) await authClient.revokeRefresh(refreshToken, req.trace);
      await sessions.destroy(req.session.sid);
    }
    clearAuthCookies(reply, config);
    return reply.code(200).send({ authenticated: false });
  });

  // ── GET /bff/me ──────────────────────────────────────────────────────────────────
  app.get('/bff/me', async (req: FastifyRequest, reply: FastifyReply) => {
    await resolveSession(req, sessions, config.cookie.sessionName);
    if (!req.session) {
      return reply.code(401).send({
        authenticated: false,
        error: { code: 'UNAUTHENTICATED', message: 'No active session' },
      });
    }
    const { data } = req.session;
    // Keep the CSRF cookie alive for the remaining session lifetime (not just the idle TTL), so a
    // long-lived silently-refreshed session doesn't lose its CSRF cookie and start 403-ing mutations.
    const csrfMaxAge = data.refreshExpiresAt
      ? Math.max(1, data.refreshExpiresAt - Math.floor(Date.now() / 1000))
      : config.sessionTtlSeconds;
    setCsrfCookie(reply, config, data.csrfToken, csrfMaxAge);
    return reply.code(200).send({
      authenticated: true,
      tenant_id: data.tenantId,
      agent_id: data.agentId,
      scopes: data.scopes,
      csrf_token: data.csrfToken,
    });
  });

  /** Map an AuthExchangeError (or unknown) to a sanitised Contract-2 reply. */
  function handleAuthError(err: unknown, reply: FastifyReply): FastifyReply {
    if (err instanceof AuthExchangeError) {
      metrics.loginAttempts.inc({ outcome: err.status === 401 ? 'invalid' : 'error' });
      const status = err.status === 401 ? 401 : err.status === 409 ? 409 : err.status >= 500 ? 502 : err.status;
      const code =
        status === 401
          ? 'INVALID_CREDENTIALS'
          : status === 409
            ? 'ALREADY_EXISTS'
            : 'AUTH_UPSTREAM_ERROR';
      const message =
        status === 401
          ? 'Invalid email or password'
          : status === 409
            ? 'An account with this email already exists'
            : 'Authentication unavailable';
      return reply.code(status).send({ error: { code, message } });
    }
    log.error({ err: (err as Error).message }, 'auth: unexpected error');
    metrics.loginAttempts.inc({ outcome: 'error' });
    return reply.code(500).send({ error: { code: 'INTERNAL', message: 'Authentication failed' } });
  }
}
