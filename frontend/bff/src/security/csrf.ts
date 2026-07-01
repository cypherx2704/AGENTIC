/**
 * CSRF protection — the double-submit-cookie pattern (WP13 §2).
 *
 * Mechanism:
 *   - On login the BFF sets a non-httpOnly `CSRF_COOKIE_NAME` cookie holding a
 *     random token, and binds the SAME token into the encrypted server-side session.
 *   - The SPA reads that cookie and echoes the value in the `X-CSRF-Token` header on
 *     every mutating request.
 *   - This hook, on every POST/PUT/DELETE/PATCH, requires:
 *         header token === cookie token === session token
 *     (a timing-safe triple match). A bare double-submit (header===cookie) is
 *     vulnerable if an attacker can plant a cookie; binding to the server session
 *     closes that gap. Any miss → 403 and `csrf_violations_total` is incremented.
 *
 * Safe methods (GET/HEAD/OPTIONS) and the login endpoint itself (no session yet) are
 * exempt — login bootstraps the token.
 */
import { timingSafeEqual } from 'node:crypto';
import type { FastifyReply, FastifyRequest } from 'fastify';

const MUTATING_METHODS = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);

/**
 * Endpoints exempt from CSRF (they cannot have a prior CSRF token): login bootstraps the
 * token, and the public self-serve onboarding POSTs (signup/resend) happen pre-account with
 * no session at all. The GET /bff/onboarding/verify needs no entry — the guard already returns
 * early for safe methods.
 */
const EXEMPT_PATHS = new Set([
  '/bff/login',
  '/bff/register',
  '/bff/onboarding/signup',
  '/bff/onboarding/resend',
]);

/** Constant-time string comparison that tolerates unequal lengths without leaking it. */
export function safeEqual(a: string | undefined, b: string | undefined): boolean {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const ab = Buffer.from(a, 'utf8');
  const bb = Buffer.from(b, 'utf8');
  if (ab.length !== bb.length) {
    // Still run a comparison to keep timing roughly constant, then fail.
    timingSafeEqual(ab, ab);
    return false;
  }
  return timingSafeEqual(ab, bb);
}

export interface CsrfDeps {
  readonly headerName: string;
  readonly cookieName: string;
  readonly onViolation: (reason: string) => void;
}

/**
 * The preHandler used to enforce CSRF on mutating requests. Returns a function bound
 * to the supplied dependencies so it can be unit-tested in isolation.
 */
export function makeCsrfGuard(deps: CsrfDeps) {
  return async function csrfGuard(req: FastifyRequest, reply: FastifyReply): Promise<void> {
    const method = req.method.toUpperCase();
    if (!MUTATING_METHODS.has(method)) return;

    const path = req.url.split('?')[0] ?? '';
    if (EXEMPT_PATHS.has(path)) return;

    const headerToken = firstHeader(req.headers[deps.headerName]);
    const cookieToken = (req.cookies as Record<string, string | undefined>)[deps.cookieName];
    const sessionToken = req.session?.data.csrfToken;

    // All three must be present and equal. Without a session there's nothing to
    // protect (and no token to compare) — but a mutating call with no session is
    // itself unauthenticated; auth guards handle that. Here we only assert that,
    // whenever a session exists, the double-submit + binding holds.
    if (!sessionToken) {
      // No session: cannot validate CSRF binding. Reject mutating calls outright.
      deps.onViolation('no-session');
      void reply.code(403).send({ error: { code: 'CSRF_FORBIDDEN', message: 'No session' } });
      return;
    }

    if (!headerToken) {
      deps.onViolation('missing-header');
      void reply
        .code(403)
        .send({ error: { code: 'CSRF_FORBIDDEN', message: 'Missing CSRF token header' } });
      return;
    }

    if (!safeEqual(headerToken, cookieToken) || !safeEqual(headerToken, sessionToken)) {
      deps.onViolation('mismatch');
      void reply
        .code(403)
        .send({ error: { code: 'CSRF_FORBIDDEN', message: 'CSRF token mismatch' } });
      return;
    }
  };
}

function firstHeader(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}
