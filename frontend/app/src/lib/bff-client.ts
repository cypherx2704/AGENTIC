/**
 * The typed BFF client — the single chokepoint for every network call the SPA makes.
 *
 * Design rules (from the WP13 brief):
 *   - The SPA talks ONLY to the BFF. Service calls go through `/bff/api/<service>/...`;
 *     session/auth through `/bff/me`, `/bff/login`, `/bff/logout`.
 *   - The browser NEVER stores tokens. Auth rides on the BFF's httpOnly session cookie,
 *     so every request uses `credentials: 'include'`.
 *   - CSRF: the BFF issues a token via `/bff/me` (and a readable cookie). We echo it back
 *     in the `X-CSRF-Token` header on every mutating method (POST/PUT/PATCH/DELETE).
 *   - Errors normalize to the Contract-2 envelope so the UI renders one consistent shape.
 */

import { config } from './config';
import type { ApiErrorEnvelope, Session } from './types';

const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const CSRF_HEADER = 'X-CSRF-Token';
// Must match the BFF's CSRF cookie name (CSRF_COOKIE_NAME, default 'cypherx_csrf').
const CSRF_COOKIE = 'cypherx_csrf';

/** A normalized, throwable API error carrying the Contract-2 envelope + HTTP status. */
export class BffError extends Error {
  readonly status: number;
  readonly code: string;
  readonly envelope: ApiErrorEnvelope['error'];
  readonly requestId?: string;
  readonly traceId?: string;
  readonly details?: Record<string, unknown> | null;

  constructor(status: number, envelope: ApiErrorEnvelope['error']) {
    super(envelope.message || `Request failed (HTTP ${status})`);
    this.name = 'BffError';
    this.status = status;
    this.code = envelope.code || 'INTERNAL_ERROR';
    this.envelope = envelope;
    this.requestId = envelope.request_id;
    this.traceId = envelope.trace_id;
    this.details = envelope.details ?? null;
  }

  /** True when the failure is an auth/session problem the shell should redirect on. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** True when a guardrail blocked the operation (xAgent renders this as 422). */
  get isGuardrailViolation(): boolean {
    return this.status === 422 && this.code === 'GUARDRAIL_VIOLATION';
  }
}

/** Read a cookie value by name (browser-only; returns null on the server). */
function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

// In-memory CSRF token cached from the last /bff/me. The cookie is the source of truth
// (survives reload); this is a fast path so mutations don't have to re-read /bff/me.
let cachedCsrf: string | null = null;

export function setCsrfToken(token: string | null): void {
  cachedCsrf = token;
}

export function getCsrfToken(): string | null {
  return cachedCsrf ?? readCookie(CSRF_COOKIE);
}

// ── Global 401 interceptor ────────────────────────────────────────────────────────────
// A single subscriber (the SessionProvider) is notified whenever a *non-session-probe*
// request comes back 401 — i.e. the session expired or was destroyed mid-use. It flips
// the session to unauthenticated so the shell redirects to /login. The probe (/me) and
// the login call are excluded: their 401s are handled inline where they happen.
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

export interface RequestOptions {
  method?: string;
  /** JSON body — serialized + Content-Type set automatically. */
  body?: unknown;
  /** Extra headers (merged; never overrides credentials/CSRF handling). */
  headers?: Record<string, string>;
  /** AbortSignal for cancellation (used by long-poll + navigation). */
  signal?: AbortSignal;
  /** Query params appended to the path. Null/undefined values are skipped. */
  query?: Record<string, string | number | boolean | null | undefined>;
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const base = config.bffBase;
  const full = path.startsWith('/') ? `${base}${path}` : `${base}/${path}`;
  if (!query) return full;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== null && v !== undefined && v !== '') params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${full}?${qs}` : full;
}

/** Coerce any non-ok response into a Contract-2 envelope (services already emit it). */
async function toEnvelope(res: Response): Promise<ApiErrorEnvelope['error']> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (body && typeof body === 'object' && 'error' in body) {
    const err = (body as ApiErrorEnvelope).error;
    if (err && typeof err === 'object' && 'code' in err) return err;
  }
  // Synthesize an envelope for non-conforming errors (gateway/proxy failures).
  return {
    code: res.status === 401 ? 'UNAUTHORIZED' : res.status >= 500 ? 'SERVICE_UNAVAILABLE' : 'INTERNAL_ERROR',
    message:
      (body && typeof body === 'object' && 'message' in body && typeof (body as any).message === 'string'
        ? (body as any).message
        : res.statusText) || `Request failed (HTTP ${res.status})`,
  };
}

/**
 * Core fetch wrapper. Adds credentials, CSRF on mutations, JSON encoding, and
 * Contract-2 error normalization. Returns parsed JSON (or undefined for 204).
 */
export async function bffFetch<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = (opts.method ?? 'GET').toUpperCase();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...opts.headers,
  };

  let body: BodyInit | undefined;
  if (opts.body !== undefined && opts.body !== null) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }

  if (MUTATING_METHODS.has(method)) {
    const csrf = getCsrfToken();
    if (csrf) headers[CSRF_HEADER] = csrf;
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      credentials: 'include', // ride the httpOnly session cookie — never store tokens
      signal: opts.signal,
    });
  } catch (err) {
    // Network-level failure (BFF down, CORS, abort). Abort propagates as-is.
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new BffError(0, {
      code: 'SERVICE_UNAVAILABLE',
      message: 'Could not reach the BFF. Check your connection and that the gateway is running.',
    });
  }

  if (!res.ok) {
    // Session expiry mid-use: any proxied/service call that 401s (but not the /me probe
    // or the /login attempt) trips the global handler so the shell can redirect to login.
    if (res.status === 401 && path !== '/me' && path !== '/login' && onUnauthorized) {
      onUnauthorized();
    }
    throw new BffError(res.status, await toEnvelope(res));
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

// ── Session helpers (the shell + auth guard call these) ──────────────────────────────

/** GET /bff/me — the session probe. Caches the CSRF token for subsequent mutations. */
export async function fetchSession(signal?: AbortSignal): Promise<Session> {
  const session = await bffFetch<Session>('/me', { signal });
  setCsrfToken(session.csrf_token ?? null);
  return session;
}

/** POST /bff/login — exchange email + password for a session cookie (orchestrator JWT held by BFF). */
export async function login(email: string, password: string): Promise<Session> {
  // The BFF verifies the password at Auth and mints the tenant ORCHESTRATOR's agent JWT, storing
  // it in the encrypted session. The token never reaches the browser.
  await bffFetch('/login', { method: 'POST', body: { email, password } });
  // Re-read the session so we capture the freshly-minted CSRF token + scopes.
  return fetchSession();
}

/** 201 response from POST /bff/register — the orchestrator's initial api_key is shown ONCE. */
export interface RegisterResult {
  authenticated: boolean;
  tenant_id: string;
  orchestrator_agent_id: string;
  api_key: string;
  key_prefix: string;
  scopes: string[];
  csrf_token?: string;
}

/** POST /bff/register — self-serve signup (tenant + user + orchestrator). Auto-logs in on success. */
export async function register(body: {
  email: string;
  password: string;
  tenant_name?: string;
  full_name?: string;
}): Promise<RegisterResult> {
  const result = await bffFetch<RegisterResult>('/register', { method: 'POST', body });
  if (result.csrf_token) setCsrfToken(result.csrf_token);
  return result;
}

/** Absolute URL of the BFF's Google sign-in start (used as an `<a href>` — full-page navigation). */
export function googleLoginUrl(): string {
  return buildUrl('/auth/google');
}

/** POST /bff/logout — drop the session cookie server-side. */
export async function logout(): Promise<void> {
  await bffFetch('/logout', { method: 'POST' });
  setCsrfToken(null);
}

// ── Self-serve onboarding (public, pre-account) ────────────────────────────────────────
// These hit the BFF's un-authenticated /bff/onboarding/* passthrough (Contract-20). No session
// or CSRF token exists yet; bffFetch only attaches the CSRF header when a token is present, so
// these pre-account calls correctly send none.

/** 202 response from POST /bff/onboarding/signup — never carries a secret. */
export interface SignupResult {
  signup_id: string;
  status: string; // 'pending_verification' | 'manual_review'
  expires_at: string;
  message: string;
}

/** 200 response from GET /bff/onboarding/verify — the ONLY time the raw initial api_key is returned. */
export interface VerifyResult {
  tenant_id: string;
  tenant_name: string;
  plan: string;
  agent_id: string;
  api_key_id: string;
  api_key: string;
  key_prefix: string;
  next?: string;
}

/** POST /bff/onboarding/signup — begin self-serve tenant registration (queues a verification email). */
export async function signup(body: {
  email: string;
  tenant_name: string;
  captcha_token: string;
}): Promise<SignupResult> {
  return bffFetch<SignupResult>('/onboarding/signup', { method: 'POST', body });
}

/** POST /bff/onboarding/resend — re-send the verification email (always 202, anti-enumeration). */
export async function resendVerification(email: string): Promise<{ message: string }> {
  return bffFetch<{ message: string }>('/onboarding/resend', { method: 'POST', body: { email } });
}

/** GET /bff/onboarding/verify — consume the token; provisions the tenant + first agent + api_key. */
export async function verifyTenant(token: string, signal?: AbortSignal): Promise<VerifyResult> {
  return bffFetch<VerifyResult>('/onboarding/verify', { query: { token }, signal });
}

// ── Service proxy helper ──────────────────────────────────────────────────────────────

/**
 * Call a platform service through the BFF proxy: `/bff/api/<service>/<rest>`.
 * `service` is one of auth | llms | guardrails | xagent | rag.
 */
export function api<T = unknown>(
  service: 'auth' | 'llms' | 'guardrails' | 'xagent' | 'rag' | 'tools',
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const rest = path.startsWith('/') ? path : `/${path}`;
  return bffFetch<T>(`/api/${service}${rest}`, opts);
}

/** Build the absolute URL for an SSE stream (EventSource can't go through bffFetch). */
export function streamUrl(service: string, path: string): string {
  const rest = path.startsWith('/') ? path : `/${path}`;
  return buildUrl(`/api/${service}${rest}`);
}
