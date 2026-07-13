/**
 * Server-side session shape. This data lives ONLY in encrypted Valkey records — it
 * never crosses to the browser. The browser holds an opaque session id (the cookie)
 * plus a CSRF token; everything else (tenant, scopes, and the downstream platform
 * token) stays here, behind the BFF.
 */
export interface SessionData {
  /** tenant the authenticated principal belongs to */
  readonly tenantId: string;
  /** agent id whose credential was exchanged at login (the tenant's orchestrator) */
  readonly agentId: string;
  /** the end-user this session belongs to (email/password or Google login); absent for legacy api_key login */
  readonly userId?: string;
  /** effective scopes granted by Auth at token-exchange time */
  readonly scopes: readonly string[];

  /**
   * The downstream platform/agent JWT the BFF injects as `Authorization: Bearer`
   * on proxied calls. SECURITY: this MUST NEVER be serialised into any response
   * sent to the browser.
   */
  readonly downstreamToken: string;
  /** absolute epoch-seconds at which the downstream token expires */
  readonly tokenExpiresAt: number;

  /**
   * Opaque refresh token (`<session_id>.<secret>`) the BFF replays to `POST /v1/auth/refresh` to
   * silently re-mint `downstreamToken` before it expires. Like `downstreamToken` it NEVER crosses to
   * the browser. Absent for legacy api_key logins (which keep the old hard-expiry behaviour).
   */
  readonly refreshToken?: string;
  /** absolute epoch-seconds hard cap of the refresh token (the whole session's max lifetime) */
  readonly refreshExpiresAt?: number;

  /** double-submit CSRF token bound to this session */
  readonly csrfToken: string;

  /** epoch-millis the session was first created */
  readonly createdAt: number;
}

/** The session id + its decrypted data, as returned by the store on read. */
export interface LoadedSession {
  readonly sid: string;
  readonly data: SessionData;
}
