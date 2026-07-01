/**
 * Shared helper: drive a successful login against a TestApp and return the session
 * cookie value + CSRF token the SPA would hold.
 */
import { parseSetCookies, type TestApp } from './testApp.js';

export const DOWNSTREAM_TOKEN = 'eyJ.SECRET-DOWNSTREAM-TOKEN.sig';

export interface LoggedIn {
  sid: string;
  csrf: string;
  tenantId: string;
}

export async function login(
  t: TestApp,
  opts: { tenantId?: string; scopes?: string[] } = {},
): Promise<LoggedIn> {
  const tenantId = opts.tenantId ?? 'tenant-xyz';
  const scopes = opts.scopes ?? ['agent:execute', 'llm:invoke'];

  // The first responder handles the email/password login (Auth mints the orchestrator JWT);
  // the caller can override the responder afterwards for the proxy target.
  t.upstream.setResponder(() => ({
    status: 200,
    body: JSON.stringify({
      user_id: 'user-1',
      tenant_id: tenantId,
      agent_id: 'orch-1',
      token: DOWNSTREAM_TOKEN,
      token_type: 'Bearer',
      expires_in: 3600,
      scopes,
    }),
  }));

  const res = await t.app.inject({
    method: 'POST',
    url: '/bff/login',
    payload: { email: 'user@example.com', password: 'hunter2pw' },
  });
  if (res.statusCode !== 200) {
    throw new Error(`login failed in helper: ${res.statusCode} ${res.body}`);
  }
  const sid = parseSetCookies(res.headers['set-cookie'])['cypherx_sid']!.value;
  const csrf = res.json().csrf_token as string;
  return { sid, csrf, tenantId };
}
