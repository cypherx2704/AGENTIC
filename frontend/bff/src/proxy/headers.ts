/**
 * Header handling for the downstream proxy (WP13 §5).
 *
 * Two directions:
 *   - sanitiseInbound: strip hop-by-hop headers + any client-supplied auth/identity
 *     headers from the incoming browser request before it goes upstream. The browser
 *     must NOT be able to spoof Authorization / X-Tenant-ID — the BFF is the sole
 *     authority on those.
 *   - buildUpstreamHeaders: inject the trusted identity headers the platform expects:
 *         Authorization: Bearer <session downstream token>
 *         X-Tenant-ID:   <session tenant>
 *         X-Request-ID:  <trace request id>
 *         traceparent:   <trace context>
 *
 * Hop-by-hop headers (RFC 7230 §6.1) are never forwarded in either direction.
 */
import type { TraceContext } from '../security/trace.js';

/** RFC 7230 hop-by-hop headers — must not be forwarded end-to-end. */
const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

/**
 * Client-controlled headers the BFF asserts authority over. These are dropped from
 * the inbound request so a malicious SPA/browser can never inject its own identity.
 */
const CLIENT_FORBIDDEN = new Set([
  'authorization',
  'x-tenant-id',
  'x-agent-id',
  'x-forwarded-agent-jwt',
  'x-bootstrap-token',
  'x-service-name',
  'cookie',
  'host',
  'content-length',
]);

export type HeaderBag = Record<string, string | string[] | undefined>;

/**
 * Produce a clean copy of inbound headers safe to forward: hop-by-hop removed,
 * client-forbidden identity headers removed. Header names are lower-cased.
 */
export function sanitiseInbound(headers: HeaderBag): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [rawKey, rawVal] of Object.entries(headers)) {
    if (rawVal === undefined) continue;
    const key = rawKey.toLowerCase();
    if (HOP_BY_HOP.has(key) || CLIENT_FORBIDDEN.has(key)) continue;
    out[key] = Array.isArray(rawVal) ? rawVal.join(', ') : rawVal;
  }
  return out;
}

export interface InjectIdentity {
  readonly downstreamToken: string;
  readonly tenantId: string;
  readonly trace: TraceContext;
}

/**
 * Merge the sanitised inbound headers with the BFF-asserted identity headers. The
 * injected values always win (they are set last), so no inbound header can override
 * the trusted identity.
 */
export function buildUpstreamHeaders(
  inbound: Record<string, string>,
  identity: InjectIdentity,
): Record<string, string> {
  return {
    ...inbound,
    authorization: `Bearer ${identity.downstreamToken}`,
    'x-tenant-id': identity.tenantId,
    'x-request-id': identity.trace.requestId,
    traceparent: identity.trace.traceparent,
  };
}

/**
 * Filter upstream response headers before they reach the browser: drop hop-by-hop
 * headers and anything that could leak server identity or re-set cookies from
 * upstream (the BFF owns the browser's cookies).
 */
const RESPONSE_FORBIDDEN = new Set(['transfer-encoding', 'connection', 'keep-alive', 'set-cookie']);

export function sanitiseUpstreamResponseHeaders(
  get: (name: string) => string | null,
  forEach: (cb: (value: string, key: string) => void) => void,
): Record<string, string> {
  const out: Record<string, string> = {};
  forEach((value, key) => {
    const k = key.toLowerCase();
    if (HOP_BY_HOP.has(k) || RESPONSE_FORBIDDEN.has(k)) return;
    out[k] = value;
  });
  // content-type is the one we always want to preserve verbatim if present.
  const ct = get('content-type');
  if (ct) out['content-type'] = ct;
  return out;
}

export { HOP_BY_HOP, CLIENT_FORBIDDEN };
