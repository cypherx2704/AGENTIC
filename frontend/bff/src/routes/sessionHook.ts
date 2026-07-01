/**
 * Session resolution + the authn guard.
 *
 *   - resolveSession: read the opaque session-id cookie, load + decrypt the session
 *     from Valkey, and attach it to the request (sliding TTL refreshed on read). Safe
 *     to call multiple times; it memoises onto req.session.
 *   - requireSession: 401 if no valid session is attached.
 *
 * The session is attached as `req.session` and is the ONLY source of truth for the
 * tenant id, scopes, and the downstream token — none of which the browser can see.
 */
import type { FastifyReply, FastifyRequest } from 'fastify';
import type { SessionStore } from '../session/store.js';

export async function resolveSession(
  req: FastifyRequest,
  sessions: SessionStore,
  cookieName: string,
): Promise<void> {
  if (req.session) return; // already resolved this request
  const cookies = (req.cookies ?? {}) as Record<string, string | undefined>;
  const sid = cookies[cookieName];
  if (!sid) return;
  const loaded = await sessions.read(sid);
  if (!loaded) return;

  // Enforce the downstream token's absolute expiry at the boundary. Once the agent JWT
  // has expired the session is effectively dead: forwarding it would only earn a 401
  // deep in the platform. Drop the stale record and treat the request as anonymous so
  // /bff/me and the proxy both return a clean, interceptable 401 (→ SPA redirects to login).
  const nowSeconds = Math.floor(Date.now() / 1000);
  if (loaded.data.tokenExpiresAt && loaded.data.tokenExpiresAt <= nowSeconds) {
    await sessions.destroy(loaded.sid);
    return;
  }

  req.session = loaded;
}

/** Returns the session, or sends a 401 and returns null. */
export function requireSession(req: FastifyRequest, reply: FastifyReply) {
  if (req.session) return req.session;
  void reply
    .code(401)
    .send({ error: { code: 'UNAUTHENTICATED', message: 'No active session' } });
  return null;
}
