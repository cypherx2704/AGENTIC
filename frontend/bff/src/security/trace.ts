/**
 * Request-id + W3C traceparent generation/propagation.
 *
 * The BFF stamps every inbound request with a request id (honouring an inbound
 * `X-Request-ID` when present) and a W3C `traceparent` (propagating an inbound one,
 * else generating a fresh trace). These are surfaced on the request and re-emitted
 * downstream by the proxy + echoed on the response so the SPA and platform share one
 * correlation id.
 */
import { randomBytes } from 'node:crypto';

const TRACEPARENT_RE = /^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

export interface TraceContext {
  readonly requestId: string;
  readonly traceparent: string;
  readonly traceId: string;
}

function randomHex(bytes: number): string {
  return randomBytes(bytes).toString('hex');
}

/** Build (or propagate) the trace context for an inbound request. */
export function deriveTraceContext(
  inboundRequestId: string | undefined,
  inboundTraceparent: string | undefined,
): TraceContext {
  const requestId =
    inboundRequestId && inboundRequestId.trim() !== ''
      ? inboundRequestId.trim().slice(0, 200)
      : randomHex(16);

  const match = inboundTraceparent ? TRACEPARENT_RE.exec(inboundTraceparent.trim()) : null;
  if (match) {
    // Propagate the inbound trace id; mint a fresh child span id for this hop.
    const traceId = match[1] as string;
    const spanId = randomHex(8);
    const flags = match[3] as string;
    return { requestId, traceparent: `00-${traceId}-${spanId}-${flags}`, traceId };
  }

  // Start a new trace. Sampled flag (01) on by default.
  const traceId = randomHex(16);
  const spanId = randomHex(8);
  return { requestId, traceparent: `00-${traceId}-${spanId}-01`, traceId };
}
