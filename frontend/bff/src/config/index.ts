/**
 * Centralised, fail-fast configuration. 100% env-driven — there are NO hardcoded
 * service URLs, secrets, or cookie names anywhere else in the service. Everything
 * the BFF needs is parsed (and validated) here exactly once at boot.
 *
 * Security-critical invariants enforced at load time:
 *   - SESSION_KEK_BASE64 must decode to exactly 32 bytes (AES-256 key).
 *   - VALKEY_URL must be present (server-side sessions are mandatory).
 *   - At least the AUTH_URL upstream must be configured (login needs it).
 *   - COOKIE_SAMESITE=none requires COOKIE_SECURE=true (browser rule + safety).
 */

export type SameSite = 'strict' | 'lax' | 'none';

export interface UpstreamConfig {
  /** logical service name, used as the `/bff/api/<name>/...` route prefix */
  readonly name: string;
  /** base URL of the upstream platform service */
  readonly baseUrl: string;
}

export interface Config {
  readonly env: string;
  readonly isProduction: boolean;
  readonly host: string;
  readonly port: number;
  readonly logLevel: string;
  readonly allowedOrigins: readonly string[];

  readonly valkeyUrl: string;
  readonly sessionKeyPrefix: string;
  readonly sessionTtlSeconds: number;

  /** raw 32-byte AES-256-GCM key-encryption-key */
  readonly sessionKek: Buffer;

  readonly cookie: {
    readonly sessionName: string;
    readonly csrfName: string;
    readonly sameSite: SameSite;
    readonly secure: boolean;
    readonly path: string;
    readonly domain: string | undefined;
  };

  readonly csrf: {
    readonly headerName: string;
  };

  readonly securityHeaders: {
    readonly csp: string;
    readonly hstsMaxAge: number;
    readonly referrerPolicy: string;
  };

  /** logical-name -> upstream base URL */
  readonly upstreams: Readonly<Record<string, string>>;
  readonly upstreamTimeoutMs: number;
  /**
   * Dedicated (longer) timeout for the onboarding verify call. Verify provisions a tenant + agent +
   * key server-side and is the one disclosure of the initial key, so the BFF must NOT abandon it
   * before Auth can finish under a slow-DB blip — its budget must exceed Auth's worst-case (incl. the
   * Hikari connection-acquire timeout). Kept separate from the normal proxy timeout.
   */
  readonly onboardingVerifyTimeoutMs: number;

  readonly dashboardCacheTtlSeconds: number;

  /** Where to send the browser after a successful Google login (SPA landing page). */
  readonly postLoginRedirect: string;
}

class ConfigError extends Error {}

type Env = Record<string, string | undefined>;

function required(env: Env, key: string): string {
  const v = env[key];
  if (v === undefined || v.trim() === '') {
    throw new ConfigError(`Missing required env var: ${key}`);
  }
  return v.trim();
}

function optional(env: Env, key: string, fallback: string): string {
  const v = env[key];
  return v === undefined || v.trim() === '' ? fallback : v.trim();
}

function asInt(value: string, key: string): number {
  const n = Number.parseInt(value, 10);
  if (!Number.isFinite(n) || n < 0) {
    throw new ConfigError(`Invalid integer for ${key}: ${value}`);
  }
  return n;
}

function asBool(value: string, key: string): boolean {
  const v = value.toLowerCase();
  if (v === 'true' || v === '1' || v === 'yes') return true;
  if (v === 'false' || v === '0' || v === 'no') return false;
  throw new ConfigError(`Invalid boolean for ${key}: ${value}`);
}

function asSameSite(value: string, key: string): SameSite {
  const v = value.toLowerCase();
  if (v === 'strict' || v === 'lax' || v === 'none') return v;
  throw new ConfigError(`Invalid ${key} (expected strict|lax|none): ${value}`);
}

function parseKek(env: Env): Buffer {
  const raw = required(env, 'SESSION_KEK_BASE64');
  let buf: Buffer;
  try {
    buf = Buffer.from(raw, 'base64');
  } catch {
    throw new ConfigError('SESSION_KEK_BASE64 is not valid base64');
  }
  if (buf.length !== 32) {
    throw new ConfigError(
      `SESSION_KEK_BASE64 must decode to 32 bytes (got ${buf.length}). ` +
        'Generate one with: node -e "console.log(require(\'crypto\').randomBytes(32).toString(\'base64\'))"',
    );
  }
  return buf;
}

function parseOrigins(value: string): readonly string[] {
  return value
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * Build the upstream registry from env. Only AUTH_URL is mandatory (login needs
 * it); the rest are optional so the BFF can run in front of a partial platform.
 */
function parseUpstreams(env: Env): Record<string, string> {
  const spec: ReadonlyArray<readonly [string, string, boolean]> = [
    ['auth', 'AUTH_URL', true],
    ['llms', 'LLMS_URL', false],
    ['guardrails', 'GUARDRAILS_URL', false],
    ['xagent', 'XAGENT_URL', false],
    ['rag', 'RAG_URL', false],
    ['tools', 'TOOL_REGISTRY_URL', false],
  ];
  const out: Record<string, string> = {};
  for (const [name, key, mandatory] of spec) {
    const v = env[key];
    if (v === undefined || v.trim() === '') {
      if (mandatory) throw new ConfigError(`Missing required upstream env var: ${key}`);
      continue;
    }
    out[name] = v.trim().replace(/\/+$/, '');
  }
  return out;
}

/**
 * Parse + validate the whole configuration from an env bag (defaults to
 * `process.env`). Throws {@link ConfigError} on any invalid/missing value so the
 * process refuses to boot misconfigured.
 */
export function loadConfig(env: Env = process.env): Config {
  const nodeEnv = optional(env, 'NODE_ENV', 'development');
  const isProduction = nodeEnv === 'production';

  const secure = asBool(optional(env, 'COOKIE_SECURE', 'false'), 'COOKIE_SECURE');
  const sameSite = asSameSite(optional(env, 'COOKIE_SAMESITE', 'lax'), 'COOKIE_SAMESITE');

  // Browser rule: SameSite=None cookies MUST be Secure. Enforce it rather than
  // silently emitting a cookie the browser will reject.
  if (sameSite === 'none' && !secure) {
    throw new ConfigError('COOKIE_SAMESITE=none requires COOKIE_SECURE=true');
  }

  const domain = optional(env, 'COOKIE_DOMAIN', '');

  const config: Config = {
    env: nodeEnv,
    isProduction,
    host: optional(env, 'BFF_HOST', '0.0.0.0'),
    port: asInt(optional(env, 'BFF_PORT', '8088'), 'BFF_PORT'),
    logLevel: optional(env, 'LOG_LEVEL', 'info'),
    allowedOrigins: parseOrigins(optional(env, 'BFF_ALLOWED_ORIGINS', '')),

    valkeyUrl: required(env, 'VALKEY_URL'),
    sessionKeyPrefix: optional(env, 'SESSION_KEY_PREFIX', 'cypherx:bff:sess:'),
    sessionTtlSeconds: asInt(optional(env, 'SESSION_TTL_SECONDS', '3600'), 'SESSION_TTL_SECONDS'),

    sessionKek: parseKek(env),

    cookie: {
      sessionName: optional(env, 'SESSION_COOKIE_NAME', 'cypherx_sid'),
      csrfName: optional(env, 'CSRF_COOKIE_NAME', 'cypherx_csrf'),
      sameSite,
      secure,
      path: optional(env, 'COOKIE_PATH', '/'),
      domain: domain === '' ? undefined : domain,
    },

    csrf: {
      headerName: optional(env, 'CSRF_HEADER_NAME', 'x-csrf-token').toLowerCase(),
    },

    securityHeaders: {
      csp: optional(
        env,
        'CSP_POLICY',
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'",
      ),
      hstsMaxAge: asInt(optional(env, 'HSTS_MAX_AGE', '31536000'), 'HSTS_MAX_AGE'),
      referrerPolicy: optional(env, 'REFERRER_POLICY', 'no-referrer'),
    },

    upstreams: parseUpstreams(env),
    upstreamTimeoutMs: asInt(optional(env, 'UPSTREAM_TIMEOUT_MS', '30000'), 'UPSTREAM_TIMEOUT_MS'),
    onboardingVerifyTimeoutMs: asInt(
      optional(env, 'ONBOARDING_VERIFY_TIMEOUT_MS', '90000'),
      'ONBOARDING_VERIFY_TIMEOUT_MS',
    ),

    dashboardCacheTtlSeconds: asInt(
      optional(env, 'DASHBOARD_CACHE_TTL_SECONDS', '30'),
      'DASHBOARD_CACHE_TTL_SECONDS',
    ),

    postLoginRedirect: optional(env, 'SPA_POST_LOGIN_URL', '/'),
  };

  return config;
}

export { ConfigError };
