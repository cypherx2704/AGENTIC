/**
 * Cookie helpers — the single place that knows how the BFF's cookies are shaped.
 *
 *   - session cookie: httpOnly + Secure + SameSite, holds ONLY the opaque session id
 *   - csrf cookie:     NON-httpOnly (the SPA must read it) + Secure + SameSite, holds
 *     the double-submit CSRF token
 *
 * Both honour the env-driven secure/sameSite/path/domain settings from config.
 */
import type { FastifyReply } from 'fastify';
import type { Config } from '../config/index.js';

interface BaseCookieOpts {
  path: string;
  sameSite: 'strict' | 'lax' | 'none';
  secure: boolean;
  domain?: string;
  maxAge?: number;
}

function baseOpts(config: Config, maxAgeSeconds?: number): BaseCookieOpts {
  const opts: BaseCookieOpts = {
    path: config.cookie.path,
    sameSite: config.cookie.sameSite,
    secure: config.cookie.secure,
  };
  if (config.cookie.domain) opts.domain = config.cookie.domain;
  if (maxAgeSeconds !== undefined) opts.maxAge = maxAgeSeconds;
  return opts;
}

/** Set the opaque session-id cookie (httpOnly — the browser can never read it). */
export function setSessionCookie(reply: FastifyReply, config: Config, sid: string): void {
  reply.setCookie(config.cookie.sessionName, sid, {
    ...baseOpts(config, config.sessionTtlSeconds),
    httpOnly: true,
  });
}

/** Set the CSRF cookie (NOT httpOnly so the SPA can echo it in the header). */
export function setCsrfCookie(reply: FastifyReply, config: Config, token: string): void {
  reply.setCookie(config.cookie.csrfName, token, {
    ...baseOpts(config, config.sessionTtlSeconds),
    httpOnly: false,
  });
}

/** Clear both cookies on logout. */
export function clearAuthCookies(reply: FastifyReply, config: Config): void {
  const clearOpts = baseOpts(config);
  reply.clearCookie(config.cookie.sessionName, { ...clearOpts, httpOnly: true });
  reply.clearCookie(config.cookie.csrfName, { ...clearOpts, httpOnly: false });
}
