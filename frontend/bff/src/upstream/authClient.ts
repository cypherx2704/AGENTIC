/**
 * Thin client for the CypherX Auth service — the only upstream the BFF calls
 * directly (everything else is opaquely proxied). Implements the platform-credential
 * exchange used by `POST /bff/login`:
 *
 *   POST {AUTH_URL}/v1/agents/{agent_id}/token
 *   headers: X-Tenant-ID: <tenant>, X-Request-ID, traceparent
 *   body:    { api_key, scopes? }
 *   200  ->  { token, token_type, expires_in, scopes }
 *
 * (Confirmed against Shared Core/auth TokenController — the token endpoint is
 * permit-all and body-authenticates the raw api_key; the tenant comes from the
 * X-Tenant-ID header, never the body.)
 */
import type { FetchLike } from '../context.js';
import type { TraceContext } from '../security/trace.js';

export interface ExchangeRequest {
  readonly tenantId: string;
  readonly agentId: string;
  readonly apiKey: string;
  readonly scopes?: readonly string[];
}

export interface ExchangeResult {
  readonly token: string;
  readonly tokenType: string;
  readonly expiresIn: number;
  readonly scopes: readonly string[];
}

/** Result of an end-user login / Google exchange — carries the identity the session is built from. */
export interface UserSessionResult {
  readonly userId: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly token: string;
  readonly tokenType: string;
  readonly expiresIn: number;
  readonly scopes: readonly string[];
}

/** Result of a self-serve registration — the orchestrator's initial api_key is shown ONCE. */
export interface RegisterResult {
  readonly userId: string;
  readonly tenantId: string;
  readonly orchestratorAgentId: string;
  readonly apiKeyId: string;
  readonly apiKey: string;
  readonly keyPrefix: string;
}

export class AuthExchangeError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export class AuthClient {
  constructor(
    private readonly baseUrl: string,
    private readonly fetch: FetchLike,
    private readonly timeoutMs: number,
  ) {}

  /** Exchange a platform/admin api_key for a short-lived agent JWT. */
  async exchangeCredential(req: ExchangeRequest, trace: TraceContext): Promise<ExchangeResult> {
    const url = `${this.baseUrl}/v1/agents/${encodeURIComponent(req.agentId)}/token`;
    const body: Record<string, unknown> = { api_key: req.apiKey };
    if (req.scopes && req.scopes.length > 0) body.scopes = req.scopes;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let res;
    try {
      res = await this.fetch(url, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-tenant-id': req.tenantId,
          'x-request-id': trace.requestId,
          traceparent: trace.traceparent,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (err) {
      throw new AuthExchangeError(502, `Auth service unreachable: ${(err as Error).message}`);
    } finally {
      clearTimeout(timer);
    }

    const raw = await res.text();
    if (res.status !== 200) {
      // Surface a sanitised reason; never echo a raw upstream error blob to the SPA.
      throw new AuthExchangeError(
        res.status === 401 || res.status === 403 ? 401 : res.status,
        extractMessage(raw) ?? 'Credential exchange failed',
      );
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new AuthExchangeError(502, 'Malformed token response from Auth');
    }
    const obj = parsed as Record<string, unknown>;
    const token = typeof obj.token === 'string' ? obj.token : undefined;
    if (!token) throw new AuthExchangeError(502, 'Auth response missing token');

    const scopes = Array.isArray(obj.scopes)
      ? (obj.scopes.filter((s) => typeof s === 'string') as string[])
      : [];
    const expiresIn = typeof obj.expires_in === 'number' ? obj.expires_in : 3600;
    const tokenType = typeof obj.token_type === 'string' ? obj.token_type : 'Bearer';

    return { token, tokenType, expiresIn, scopes };
  }

  /** Email/password login — Auth mints the tenant orchestrator's JWT. */
  async loginWithPassword(
    email: string,
    password: string,
    trace: TraceContext,
  ): Promise<UserSessionResult> {
    const obj = await this.postJson('/v1/auth/login', { email, password }, trace);
    return this.toSession(obj);
  }

  /** Self-serve registration — provisions tenant + user + orchestrator + initial api_key. */
  async register(
    body: { email: string; password: string; tenant_name?: string; full_name?: string },
    trace: TraceContext,
  ): Promise<RegisterResult> {
    const obj = await this.postJson('/v1/auth/register', body, trace);
    const s = (k: string): string => (typeof obj[k] === 'string' ? (obj[k] as string) : '');
    if (!s('user_id') || !s('tenant_id') || !s('orchestrator_agent_id')) {
      throw new AuthExchangeError(502, 'Auth register response missing identity fields');
    }
    return {
      userId: s('user_id'),
      tenantId: s('tenant_id'),
      orchestratorAgentId: s('orchestrator_agent_id'),
      apiKeyId: s('api_key_id'),
      apiKey: s('api_key'),
      keyPrefix: s('key_prefix'),
    };
  }

  /** Fetch the Google consent URL (Auth stashes the single-use state in Valkey). */
  async googleAuthUrl(trace: TraceContext): Promise<string> {
    const url = `${this.baseUrl}/v1/auth/oauth2/google`;
    const res = await this.call(url, 'GET', undefined, trace);
    const raw = await res.text();
    if (res.status !== 200) {
      throw new AuthExchangeError(res.status >= 500 ? 502 : res.status, extractMessage(raw) ?? 'Google start failed');
    }
    const obj = this.parse(raw);
    const authUrl = typeof obj.auth_url === 'string' ? obj.auth_url : undefined;
    if (!authUrl) throw new AuthExchangeError(502, 'Auth response missing auth_url');
    return authUrl;
  }

  /** Exchange the Google authorization code → orchestrator session. */
  async exchangeGoogleCode(
    code: string,
    state: string,
    trace: TraceContext,
  ): Promise<UserSessionResult> {
    const obj = await this.postJson('/v1/auth/oauth2/google/callback', { code, state }, trace);
    return this.toSession(obj);
  }

  // ── internals ──────────────────────────────────────────────────────────────────

  private async postJson(
    path: string,
    body: Record<string, unknown>,
    trace: TraceContext,
  ): Promise<Record<string, unknown>> {
    const res = await this.call(`${this.baseUrl}${path}`, 'POST', JSON.stringify(body), trace);
    const raw = await res.text();
    if (res.status < 200 || res.status >= 300) {
      throw new AuthExchangeError(
        res.status === 401 || res.status === 403 ? res.status : res.status >= 500 ? 502 : res.status,
        extractMessage(raw) ?? 'Auth request failed',
      );
    }
    return this.parse(raw);
  }

  private async call(url: string, method: string, body: string | undefined, trace: TraceContext) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await this.fetch(url, {
        method,
        headers: {
          'content-type': 'application/json',
          'x-request-id': trace.requestId,
          traceparent: trace.traceparent,
        },
        body,
        signal: controller.signal,
      });
    } catch (err) {
      throw new AuthExchangeError(502, `Auth service unreachable: ${(err as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
  }

  private parse(raw: string): Record<string, unknown> {
    try {
      return JSON.parse(raw) as Record<string, unknown>;
    } catch {
      throw new AuthExchangeError(502, 'Malformed response from Auth');
    }
  }

  private toSession(obj: Record<string, unknown>): UserSessionResult {
    const str = (k: string): string => (typeof obj[k] === 'string' ? (obj[k] as string) : '');
    const token = str('token');
    if (!token) throw new AuthExchangeError(502, 'Auth response missing token');
    const scopes = Array.isArray(obj.scopes)
      ? (obj.scopes.filter((x) => typeof x === 'string') as string[])
      : [];
    return {
      userId: str('user_id'),
      tenantId: str('tenant_id'),
      agentId: str('agent_id'),
      token,
      tokenType: str('token_type') || 'Bearer',
      expiresIn: typeof obj.expires_in === 'number' ? (obj.expires_in as number) : 3600,
      scopes,
    };
  }
}

function extractMessage(raw: string): string | undefined {
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const err = parsed.error;
    if (typeof err === 'string') return err;
    if (err && typeof err === 'object') {
      const m = (err as Record<string, unknown>).message;
      if (typeof m === 'string') return m;
    }
    if (typeof parsed.message === 'string') return parsed.message;
  } catch {
    /* not JSON */
  }
  return undefined;
}
