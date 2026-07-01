import { describe, it, expect, afterEach } from 'vitest';
import { makeTestApp, type TestApp } from './helpers/testApp.js';

describe('security headers on every response', () => {
  let t: TestApp;

  afterEach(async () => {
    if (t) await t.app.close();
  });

  async function assertCommonHeaders(headers: Record<string, unknown>): Promise<void> {
    expect(headers['content-security-policy']).toBeDefined();
    expect(headers['x-frame-options']).toBe('DENY');
    expect(headers['x-content-type-options']).toBe('nosniff');
    expect(headers['referrer-policy']).toBeDefined();
    expect(headers['cross-origin-opener-policy']).toBe('same-origin');
    expect(headers['x-powered-by']).toBeUndefined();
  }

  it('applies headers to a health response', async () => {
    t = await makeTestApp();
    const res = await t.app.inject({ method: 'GET', url: '/livez' });
    expect(res.statusCode).toBe(200);
    await assertCommonHeaders(res.headers);
  });

  it('applies headers to a 404 response', async () => {
    t = await makeTestApp();
    const res = await t.app.inject({ method: 'GET', url: '/nope' });
    expect(res.statusCode).toBe(404);
    await assertCommonHeaders(res.headers);
  });

  it('applies headers to a 401 (unauthenticated proxy) response', async () => {
    t = await makeTestApp();
    const res = await t.app.inject({ method: 'GET', url: '/bff/api/llms/v1/models' });
    expect(res.statusCode).toBe(401);
    await assertCommonHeaders(res.headers);
  });

  it('emits HSTS only when COOKIE_SECURE=true', async () => {
    t = await makeTestApp();
    const insecure = await t.app.inject({ method: 'GET', url: '/livez' });
    expect(insecure.headers['strict-transport-security']).toBeUndefined();
    await t.app.close();

    t = await makeTestApp({ COOKIE_SECURE: 'true', COOKIE_SAMESITE: 'none' });
    const secure = await t.app.inject({ method: 'GET', url: '/livez' });
    expect(secure.headers['strict-transport-security']).toContain('max-age=');
  });

  it('uses the env-tunable CSP', async () => {
    t = await makeTestApp({ CSP_POLICY: "default-src 'none'" });
    const res = await t.app.inject({ method: 'GET', url: '/livez' });
    expect(res.headers['content-security-policy']).toBe("default-src 'none'");
  });

  it('echoes the correlation ids on the response', async () => {
    t = await makeTestApp();
    const res = await t.app.inject({ method: 'GET', url: '/livez' });
    expect(res.headers['x-request-id']).toBeTruthy();
    expect(res.headers['traceparent']).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/);
  });
});
