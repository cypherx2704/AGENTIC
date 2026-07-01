/**
 * Runtime configuration — 100% env-driven, no hardcoded service URLs or secrets.
 *
 * Only NEXT_PUBLIC_* vars reach the browser bundle (Next.js inlines them at build time).
 * The BFF holds every secret + the httpOnly session cookie; the SPA only knows where the
 * BFF lives and a couple of cosmetic/polling knobs.
 */

function envInt(raw: string | undefined, fallback: number): number {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** Trim a single trailing slash so we can safely concatenate path segments. */
function stripTrailingSlash(value: string): string {
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

// Base origin of the BFF. Empty => same-origin (calls go to a relative /bff/... path).
const BFF_URL = stripTrailingSlash(process.env.NEXT_PUBLIC_BFF_URL ?? '');

// Path prefix the BFF mounts under (default "/bff"). Leading slash enforced.
const rawPrefix = process.env.NEXT_PUBLIC_BFF_PREFIX ?? '/bff';
const BFF_PREFIX = stripTrailingSlash(rawPrefix.startsWith('/') ? rawPrefix : `/${rawPrefix}`);

export const config = {
  /** Full origin of the BFF, or '' for same-origin. */
  bffUrl: BFF_URL,
  /** Mount prefix of the BFF routes (e.g. '/bff'). */
  bffPrefix: BFF_PREFIX,
  /** Base for every BFF call: `${origin}${prefix}` (e.g. 'http://host:8090/bff' or '/bff'). */
  bffBase: `${BFF_URL}${BFF_PREFIX}`,
  taskFeedPollMs: envInt(process.env.NEXT_PUBLIC_TASK_FEED_POLL_MS, 5000),
  healthPollMs: envInt(process.env.NEXT_PUBLIC_HEALTH_POLL_MS, 10000),
  appName: process.env.NEXT_PUBLIC_APP_NAME ?? 'CypherX',
} as const;

export type AppConfig = typeof config;
