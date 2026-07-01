import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, type TestApp } from './helpers/testApp.js';

describe('Public onboarding passthrough (/bff/onboarding/*)', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  it('signup forwards to Auth /v1/onboarding/signup and passes the 202 through (CSRF-exempt, no session)', async () => {
    t.upstream.setResponder((call) => {
      expect(call.url).toBe('http://auth.test/v1/onboarding/signup');
      expect(call.method).toBe('POST');
      // Pre-account: NO identity headers may be injected.
      expect(call.headers['authorization']).toBeUndefined();
      expect(call.headers['x-tenant-id']).toBeUndefined();
      // The body is forwarded verbatim.
      expect(JSON.parse(call.body ?? '{}')).toEqual({
        email: 'a@b.com',
        tenant_name: 'Acme',
        captcha_token: 'mock',
      });
      return {
        status: 202,
        body: JSON.stringify({ signup_id: 's1', status: 'pending_verification', expires_at: 'x', message: 'ok' }),
      };
    });

    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/onboarding/signup',
      payload: { email: 'a@b.com', tenant_name: 'Acme', captcha_token: 'mock' },
    });

    // NOT 403 — i.e. the CSRF guard exempted this session-less POST.
    expect(res.statusCode).toBe(202);
    expect(res.json().status).toBe('pending_verification');
    expect(res.headers['cache-control']).toContain('no-store');
  });

  it('verify forwards the token as a query param and passes the 200 + one-time api_key through', async () => {
    t.upstream.setResponder((call) => {
      expect(call.method).toBe('GET');
      expect(call.url).toBe('http://auth.test/v1/onboarding/verify?token=tok-123');
      return {
        status: 200,
        body: JSON.stringify({
          tenant_id: 'ten-1',
          tenant_name: 'Acme',
          plan: 'free',
          agent_id: 'agent-1',
          api_key_id: 'k1',
          api_key: 'cx_local_RAWSECRET',
          key_prefix: 'cx_local',
        }),
      };
    });

    const res = await t.app.inject({ method: 'GET', url: '/bff/onboarding/verify?token=tok-123' });
    expect(res.statusCode).toBe(200);
    expect(res.json().api_key).toBe('cx_local_RAWSECRET');
    // The one-time key response must never be cached.
    expect(res.headers['cache-control']).toContain('no-store');
  });

  it('verify passes a 410 Gone (expired/used link) straight through', async () => {
    t.upstream.setResponder(() => ({
      status: 410,
      body: JSON.stringify({ error: { code: 'GONE', message: 'Verification link has expired; request a new one' } }),
    }));
    const res = await t.app.inject({ method: 'GET', url: '/bff/onboarding/verify?token=stale' });
    expect(res.statusCode).toBe(410);
    expect(res.json().error.code).toBe('GONE');
  });

  it('resend forwards and passes the anti-enumeration 202 through (CSRF-exempt)', async () => {
    t.upstream.setResponder((call) => {
      expect(call.url).toBe('http://auth.test/v1/onboarding/resend');
      expect(JSON.parse(call.body ?? '{}')).toEqual({ email: 'a@b.com' });
      return { status: 202, body: JSON.stringify({ message: 'If a pending signup exists, a new email has been sent.' }) };
    });
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/onboarding/resend',
      payload: { email: 'a@b.com' },
    });
    expect(res.statusCode).toBe(202);
  });

  it('a transport failure to Auth becomes a Contract-2 502', async () => {
    t.upstream.setResponder(() => {
      throw new Error('ECONNREFUSED');
    });
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/onboarding/signup',
      payload: { email: 'a@b.com', tenant_name: 'Acme', captcha_token: 'mock' },
    });
    expect(res.statusCode).toBe(502);
    expect(res.json().error.code).toBe('AUTH_UPSTREAM_ERROR');
  });

  it('the onboarding CSRF exemption is scoped — an unrelated session-less POST is still 403', async () => {
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/auth/v1/agents',
      payload: { name: 'x' },
    });
    expect(res.statusCode).toBe(403);
    expect(res.json().error.code).toBe('CSRF_FORBIDDEN');
  });
});
