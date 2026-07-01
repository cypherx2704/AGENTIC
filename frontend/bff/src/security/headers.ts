/**
 * Security headers applied to EVERY response (WP13 §3). A single onSend hook so we
 * cannot forget the headers on any route — defence-in-depth for a browser-facing
 * security boundary.
 *
 *   - Content-Security-Policy        (env-tunable; tight SPA default)
 *   - Strict-Transport-Security      (only when serving over HTTPS / secure cookies)
 *   - X-Frame-Options: DENY          (clickjacking)
 *   - X-Content-Type-Options: nosniff
 *   - Referrer-Policy                (env-tunable; no-referrer default)
 *   - Cross-Origin-Opener/Resource-Policy hardening
 *   - Cache-Control: no-store on auth responses (login/logout/me) so tokens/CSRF
 *     material is never cached by intermediaries or the browser.
 */
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import type { Config } from '../config/index.js';

/**
 * Routes whose responses must never be cached (carry auth/session material). `/bff/onboarding`
 * is included because the verify response returns the one-time raw initial api_key.
 */
const NO_STORE_PREFIXES = ['/bff/login', '/bff/logout', '/bff/me', '/bff/onboarding'];

export function registerSecurityHeaders(app: FastifyInstance, config: Config): void {
  app.addHook('onSend', async (req: FastifyRequest, reply: FastifyReply, payload: unknown) => {
    reply.header('Content-Security-Policy', config.securityHeaders.csp);
    reply.header('X-Frame-Options', 'DENY');
    reply.header('X-Content-Type-Options', 'nosniff');
    reply.header('Referrer-Policy', config.securityHeaders.referrerPolicy);
    reply.header('Cross-Origin-Opener-Policy', 'same-origin');
    reply.header('Cross-Origin-Resource-Policy', 'same-origin');
    reply.header('X-Permitted-Cross-Domain-Policies', 'none');
    // Strip the framework's server-identifying header.
    reply.removeHeader('X-Powered-By');

    // HSTS only makes sense over HTTPS; gate it on the secure-cookie posture so we
    // don't pin HTTPS on a plain-http local dev box.
    if (config.cookie.secure && config.securityHeaders.hstsMaxAge > 0) {
      reply.header(
        'Strict-Transport-Security',
        `max-age=${config.securityHeaders.hstsMaxAge}; includeSubDomains`,
      );
    }

    // No-cache for auth responses.
    const url = req.url.split('?')[0] ?? '';
    if (NO_STORE_PREFIXES.some((p) => url === p || url.startsWith(`${p}`))) {
      reply.header('Cache-Control', 'no-store, max-age=0');
      reply.header('Pragma', 'no-cache');
    }

    return payload;
  });
}
