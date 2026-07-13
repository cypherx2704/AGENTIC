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
import type { FastifyRequest, FastifyReply } from 'fastify';
import type { SessionStore } from '../session/store.js';
import type { LoadedSession, SessionData } from '../session/types.js';

/**
 * Re-mint the access token this many seconds BEFORE it actually expires. Proactive renewal keeps an
 * active user's requests from ever racing the expiry boundary.
 */
const REFRESH_SKEW_SECONDS = 120;

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

  const nowSeconds = Math.floor(Date.now() / 1000);

  // Absolute cap: the refresh window itself has ended. The session is truly over — no renewal.
  if (loaded.data.refreshExpiresAt && loaded.data.refreshExpiresAt <= nowSeconds) {
    await sessions.destroy(loaded.sid);
    return;
  }

  // The <=1h access token has expired (or is about to). If this is a user-login session (it carries a
  // refresh token), silently re-mint the access token so an ACTIVE session never hard-expires
  // mid-work — the fix for "session expired, unsaved work lost". A failed refresh (revoked / idle-
  // timed-out / auth down) means the session is dead: drop it so the request 401s and the SPA re-logs.
  const accessStale =
    loaded.data.tokenExpiresAt && loaded.data.tokenExpiresAt <= nowSeconds + REFRESH_SKEW_SECONDS;
  if (accessStale && loaded.data.refreshToken) {
    const refreshed = await tryRefresh(req, sessions, loaded);
    if (!refreshed) {
      await sessions.destroy(loaded.sid);
      return;
    }
    req.session = refreshed;
    return;
  }

  // Legacy api_key sessions (no refresh token): keep the original hard cutover at access-token expiry.
  if (loaded.data.tokenExpiresAt && loaded.data.tokenExpiresAt <= nowSeconds) {
    await sessions.destroy(loaded.sid);
    return;
  }

  req.session = loaded;
}

/**
 * Exchange the session's refresh token for a fresh access token and persist it in place (same sid).
 * Returns the updated session, or null if the refresh was rejected/failed. Never throws.
 */
async function tryRefresh(
  req: FastifyRequest,
  sessions: SessionStore,
  loaded: LoadedSession,
): Promise<LoadedSession | null> {
  const { authClient, log } = req.server.bff;
  const refreshToken = loaded.data.refreshToken;
  if (!refreshToken) return null;
  try {
    const result = await authClient.refreshSession(refreshToken, req.trace);
    const nowSeconds = Math.floor(Date.now() / 1000);
    const next: SessionData = {
      ...loaded.data,
      scopes: result.scopes.length > 0 ? result.scopes : loaded.data.scopes,
      downstreamToken: result.token,
      tokenExpiresAt: nowSeconds + result.expiresIn,
      // The refresh token is non-rotating; keep it and the fixed absolute cap unchanged.
      refreshToken: result.refreshToken || loaded.data.refreshToken,
    };
    await sessions.update(loaded.sid, next);
    return { sid: loaded.sid, data: next };
  } catch (err) {
    log.debug({ err: (err as Error).message }, 'session: silent refresh failed — dropping session');
    return null;
  }
}

/** Returns the session, or sends a 401 and returns null. */
export function requireSession(req: FastifyRequest, reply: FastifyReply) {
  if (req.session) return req.session;
  void reply
    .code(401)
    .send({ error: { code: 'UNAUTHENTICATED', message: 'No active session' } });
  return null;
}
