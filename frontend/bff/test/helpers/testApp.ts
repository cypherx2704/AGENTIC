/**
 * Test harness: build a fully-wired BFF app with an in-memory Valkey (ioredis-mock)
 * and a programmable fake fetch for upstreams. No live infra. Returns the app plus
 * handles to inspect/drive the fakes.
 */
import RedisMock from 'ioredis-mock';
import { buildApp } from '../../src/app.js';
import { loadConfig, type Config } from '../../src/config/index.js';
import type { FetchLike, UpstreamResponse } from '../../src/context.js';
import type { RedisLike } from '../../src/session/store.js';
import type { ReadinessProbe } from '../../src/routes/health.js';

/** A 32-byte KEK base64 (all-zero is fine for tests; never use in prod). */
export const TEST_KEK_B64 = Buffer.alloc(32, 7).toString('base64');

export function testEnv(overrides: Record<string, string> = {}): Record<string, string> {
  return {
    NODE_ENV: 'test',
    VALKEY_URL: 'redis://localhost:6379',
    SESSION_KEK_BASE64: TEST_KEK_B64,
    AUTH_URL: 'http://auth.test',
    LLMS_URL: 'http://llms.test',
    GUARDRAILS_URL: 'http://guardrails.test',
    XAGENT_URL: 'http://xagent.test',
    RAG_URL: 'http://rag.test',
    COOKIE_SECURE: 'false',
    COOKIE_SAMESITE: 'lax',
    SESSION_COOKIE_NAME: 'cypherx_sid',
    CSRF_COOKIE_NAME: 'cypherx_csrf',
    CSRF_HEADER_NAME: 'x-csrf-token',
    DASHBOARD_CACHE_TTL_SECONDS: '30',
    LOG_LEVEL: 'silent',
    ...overrides,
  };
}

export interface FakeUpstreamCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

export interface FakeResponseSpec {
  status?: number;
  headers?: Record<string, string>;
  body?: string;
  /** for SSE: an async-iterable of chunks */
  stream?: AsyncIterable<Uint8Array>;
}

export type Responder = (call: FakeUpstreamCall) => FakeResponseSpec | Promise<FakeResponseSpec>;

export class FakeUpstream {
  readonly calls: FakeUpstreamCall[] = [];
  private responder: Responder = () => ({ status: 200, body: '{}' });

  setResponder(r: Responder): void {
    this.responder = r;
  }

  readonly fetch: FetchLike = async (url, init) => {
    const headers = normaliseHeaders(init?.headers);
    const call: FakeUpstreamCall = {
      url,
      method: (init?.method ?? 'GET').toUpperCase(),
      headers,
      body: bodyToString(init?.body),
    };
    this.calls.push(call);
    const spec = await this.responder(call);
    return toResponse(spec);
  };

  lastCall(): FakeUpstreamCall | undefined {
    return this.calls[this.calls.length - 1];
  }
}

function normaliseHeaders(h: Record<string, string> | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!h) return out;
  for (const [k, v] of Object.entries(h)) out[k.toLowerCase()] = v;
  return out;
}

function bodyToString(body: string | Buffer | undefined): string | undefined {
  if (body === undefined) return undefined;
  return Buffer.isBuffer(body) ? body.toString('utf8') : body;
}

function toResponse(spec: FakeResponseSpec): UpstreamResponse {
  const status = spec.status ?? 200;
  const headers = new Map<string, string>();
  for (const [k, v] of Object.entries(spec.headers ?? {})) headers.set(k.toLowerCase(), v);
  if (!headers.has('content-type') && spec.body !== undefined) {
    headers.set('content-type', 'application/json');
  }
  const text = spec.body ?? '';
  return {
    status,
    headers: {
      get: (name: string) => headers.get(name.toLowerCase()) ?? null,
      forEach: (cb) => headers.forEach((value, key) => cb(value, key)),
    },
    text: async () => text,
    json: async () => JSON.parse(text || '{}'),
    body: spec.stream,
  };
}

export interface TestApp {
  app: Awaited<ReturnType<typeof buildApp>>;
  upstream: FakeUpstream;
  redis: RedisLike;
  config: Config;
  ready: { value: boolean };
  /** Read a metric value by name (optionally with labels) from the registry. */
  metricValue(name: string, labels?: Record<string, string>): number;
}

export async function makeTestApp(envOverrides: Record<string, string> = {}): Promise<TestApp> {
  const config = loadConfig(testEnv(envOverrides));
  const redis = new RedisMock() as unknown as RedisLike;
  const upstream = new FakeUpstream();
  const ready = { value: true };
  const readiness: ReadinessProbe = { ping: async () => ready.value };

  const app = await buildApp({ config, redis, fetch: upstream.fetch, readiness });
  await app.ready();

  const metricValue = (name: string, labels: Record<string, string> = {}): number => {
    const rendered = app.bff.metrics.registry.render();
    // Sum all series of the named metric (ignoring labels when none supplied) by
    // scanning the exposition text — robust against label ordering.
    let total = 0;
    for (const line of rendered.split('\n')) {
      if (line.startsWith('#') || line.trim() === '') continue;
      const m = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(-?\d+(?:\.\d+)?)$/.exec(line);
      if (!m || m[1] !== name) continue;
      const labelBlock = m[2] ?? '';
      const wanted = Object.entries(labels);
      const matches = wanted.every(([k, v]) => labelBlock.includes(`${k}="${v}"`));
      if (matches) total += Number(m[3]);
    }
    return total;
  };

  return { app, upstream, redis, config, ready, metricValue };
}

/** Parse a Set-Cookie header array into a name->{value,attrs} map. */
export function parseSetCookies(
  setCookie: string | string[] | undefined,
): Record<string, { value: string; attrs: string }> {
  const arr = Array.isArray(setCookie) ? setCookie : setCookie ? [setCookie] : [];
  const out: Record<string, { value: string; attrs: string }> = {};
  for (const c of arr) {
    const semi = c.indexOf(';');
    const pair = semi === -1 ? c : c.slice(0, semi);
    const eq = pair.indexOf('=');
    const name = pair.slice(0, eq);
    const value = pair.slice(eq + 1);
    out[name] = { value, attrs: semi === -1 ? '' : c.slice(semi + 1).toLowerCase() };
  }
  return out;
}
