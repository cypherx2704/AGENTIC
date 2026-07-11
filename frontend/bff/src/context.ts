/**
 * The shared application context — the dependency bundle threaded through routes,
 * plugins, and the proxy. Built once in `buildApp` and decorated onto the Fastify
 * instance as `app.bff`. Keeping it explicit (rather than reaching for globals or
 * module singletons) makes the whole service trivially testable: a test supplies a
 * fake Valkey + a fake upstream fetch and gets a fully-wired app with no live infra.
 */
import type { Config } from './config/index.js';
import type { SessionStore } from './session/store.js';
import type { BffMetrics } from './observability/metrics.js';
import type { Logger } from './observability/logger.js';
import type { AuthClient } from './upstream/authClient.js';
import type { DashboardCache } from './proxy/cache.js';
import type { TraceContext } from './security/trace.js';

/** The fetch signature used for all upstream calls (injectable for tests). */
export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string | Buffer | undefined;
    signal?: AbortSignal;
  },
) => Promise<UpstreamResponse>;

/** Minimal structural response — what both the WHATWG fetch and a fake return. */
export interface UpstreamResponse {
  readonly status: number;
  readonly headers: {
    get(name: string): string | null;
    forEach(cb: (value: string, key: string) => void): void;
  };
  text(): Promise<string>;
  json(): Promise<unknown>;
  /** Present on the real WHATWG fetch; used by the Node-RED editor proxy for binary-safe
   *  passthrough (fonts/images). Optional so test fakes need only implement text()/json(). */
  arrayBuffer?(): Promise<ArrayBuffer>;
  readonly body?: unknown;
}

export interface BffContext {
  readonly config: Config;
  readonly sessions: SessionStore;
  readonly metrics: BffMetrics;
  readonly log: Logger;
  readonly authClient: AuthClient;
  readonly cache: DashboardCache;
  readonly fetch: FetchLike;
}

declare module 'fastify' {
  interface FastifyInstance {
    bff: BffContext;
  }
  interface FastifyRequest {
    /** populated by the trace hook for every request */
    trace: TraceContext;
    /** populated by the session hook when a valid session cookie is present */
    session?: import('./session/types.js').LoadedSession;
  }
}
