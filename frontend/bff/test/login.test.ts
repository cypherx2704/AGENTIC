import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, parseSetCookies, type TestApp } from './helpers/testApp.js';

const VALID_TOKEN = 'eyJ.DOWNSTREAM-AGENT-JWT.sig';

describe('POST /bff/login + /bff/me + /bff/logout (session bootstrap contract)', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  function mockAuthSuccess(scopes = ['agent:execute', 'llm:invoke']): void {
    t.upstream.setResponder((call) => {
      // Email/password login resolves the tenant orchestrator and mints its JWT.
      expect(call.url).toContain('/v1/auth/login');
      return {
        status: 200,
        body: JSON.stringify({
          user_id: 'user-1',
          tenant_id: 'tenant-xyz',
          agent_id: 'orch-1',
          token: VALID_TOKEN,
          token_type: 'Bearer',
          expires_in: 3600,
          scopes,
        }),
      };
    });
  }

  it('happy path: email/password login, sets cookies, returns contract shape (no token)', async () => {
    mockAuthSuccess();
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com', password: 'hunter2pw' },
    });
    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body).toEqual({
      authenticated: true,
      tenant_id: 'tenant-xyz',
      scopes: ['agent:execute', 'llm:invoke'],
      csrf_token: expect.any(String),
    });
    // KEY CUSTODY: the downstream token must NOT appear anywhere in the response.
    expect(res.body).not.toContain(VALID_TOKEN);

    const cookies = parseSetCookies(res.headers['set-cookie']);
    expect(cookies['cypherx_sid']).toBeDefined();
    expect(cookies['cypherx_sid']!.attrs).toContain('httponly');
    expect(cookies['cypherx_sid']!.attrs).toContain('samesite=lax');
    // CSRF cookie present and NOT httpOnly (SPA must read it).
    expect(cookies['cypherx_csrf']).toBeDefined();
    expect(cookies['cypherx_csrf']!.attrs).not.toContain('httponly');
    // The session id in the cookie is opaque (not the JWT).
    expect(cookies['cypherx_sid']!.value).not.toContain('JWT');
  });

  it('invalid credentials -> 401 with generic message (no upstream leak)', async () => {
    t.upstream.setResponder(() => ({
      status: 401,
      body: JSON.stringify({ error: { code: 'INVALID_CREDENTIALS', message: 'secret internal reason' } }),
    }));
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com', password: 'wrong' },
    });
    expect(res.statusCode).toBe(401);
    expect(res.body).not.toContain('secret internal reason');
    expect(res.json().error.code).toBe('INVALID_CREDENTIALS');
    expect(res.headers['set-cookie']).toBeUndefined();
  });

  it('missing fields -> 400', async () => {
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com' },
    });
    expect(res.statusCode).toBe(400);
  });

  it('upstream unreachable/5xx -> 502, no cookie', async () => {
    t.upstream.setResponder(() => ({ status: 503, body: 'down' }));
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com', password: 'pw' },
    });
    expect(res.statusCode).toBe(502);
    expect(res.headers['set-cookie']).toBeUndefined();
  });

  it('GET /bff/me without a session -> 401 unauthenticated', async () => {
    const res = await t.app.inject({ method: 'GET', url: '/bff/me' });
    expect(res.statusCode).toBe(401);
    expect(res.json().authenticated).toBe(false);
  });

  it('full lifecycle: login -> me reflects session -> logout clears it', async () => {
    mockAuthSuccess(['agent:execute']);
    const login = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com', password: 'hunter2pw' },
    });
    const sid = parseSetCookies(login.headers['set-cookie'])['cypherx_sid']!.value;
    const csrf = login.json().csrf_token;

    const me = await t.app.inject({
      method: 'GET',
      url: '/bff/me',
      cookies: { cypherx_sid: sid },
    });
    expect(me.statusCode).toBe(200);
    expect(me.json()).toMatchObject({
      authenticated: true,
      tenant_id: 'tenant-xyz',
      scopes: ['agent:execute'],
      csrf_token: csrf,
    });
    // /bff/me must never leak the downstream token either.
    expect(me.body).not.toContain(VALID_TOKEN);

    const logout = await t.app.inject({
      method: 'POST',
      url: '/bff/logout',
      cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
      headers: { 'x-csrf-token': csrf },
    });
    expect(logout.statusCode).toBe(200);
    const cleared = parseSetCookies(logout.headers['set-cookie']);
    // Cleared cookies have an expiry in the past / empty value.
    expect(cleared['cypherx_sid']).toBeDefined();

    // After logout the session is gone.
    const meAfter = await t.app.inject({
      method: 'GET',
      url: '/bff/me',
      cookies: { cypherx_sid: sid },
    });
    expect(meAfter.statusCode).toBe(401);
  });

  it('sets the no-store cache header on auth responses', async () => {
    mockAuthSuccess();
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com', password: 'hunter2pw' },
    });
    expect(res.headers['cache-control']).toContain('no-store');
  });
});
