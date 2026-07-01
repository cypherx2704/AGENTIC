import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, type TestApp } from './helpers/testApp.js';
import { login } from './helpers/login.js';
import { safeEqual } from '../src/security/csrf.js';

describe('CSRF double-submit enforcement', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  function proxyOk(): void {
    t.upstream.setResponder(() => ({ status: 200, body: '{"ok":true}' }));
  }

  it('valid token (header===cookie===session) passes', async () => {
    const { sid, csrf } = await login(t);
    proxyOk();
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/xagent/v1/tasks',
      cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
      headers: { 'x-csrf-token': csrf, 'content-type': 'application/json' },
      payload: { input: { message: 'hi' } },
    });
    expect(res.statusCode).toBe(200);
    expect(t.metricValue('csrf_violations_total')).toBe(0);
  });

  it('missing CSRF header -> 403 + metric', async () => {
    const { sid, csrf } = await login(t);
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/xagent/v1/tasks',
      cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
      headers: { 'content-type': 'application/json' },
      payload: {},
    });
    expect(res.statusCode).toBe(403);
    expect(res.json().error.code).toBe('CSRF_FORBIDDEN');
    expect(t.metricValue('csrf_violations_total')).toBeGreaterThanOrEqual(1);
  });

  it('mismatched header vs cookie -> 403 + metric', async () => {
    const { sid, csrf } = await login(t);
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/xagent/v1/tasks',
      cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
      headers: { 'x-csrf-token': 'attacker-supplied-value', 'content-type': 'application/json' },
      payload: {},
    });
    expect(res.statusCode).toBe(403);
    expect(t.metricValue('csrf_violations_total')).toBeGreaterThanOrEqual(1);
  });

  it('attacker plants a cookie+header pair but lacks the session-bound token -> 403', async () => {
    const { sid } = await login(t);
    // Classic double-submit weakness: attacker sets cookie==header to a value they
    // chose. Our session-binding defeats it because it won't match the session token.
    const forged = 'forged-but-matching';
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/xagent/v1/tasks',
      cookies: { cypherx_sid: sid, cypherx_csrf: forged },
      headers: { 'x-csrf-token': forged, 'content-type': 'application/json' },
      payload: {},
    });
    expect(res.statusCode).toBe(403);
    expect(t.metricValue('csrf_violations_total')).toBeGreaterThanOrEqual(1);
  });

  it('mutating request with no session -> 403', async () => {
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/xagent/v1/tasks',
      headers: { 'content-type': 'application/json' },
      payload: {},
    });
    expect(res.statusCode).toBe(403);
    expect(t.metricValue('csrf_violations_total')).toBeGreaterThanOrEqual(1);
  });

  it('GET requests are exempt (safe method)', async () => {
    const { sid } = await login(t);
    t.upstream.setResponder(() => ({ status: 200, body: '{"ok":true}' }));
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/xagent/v1/tasks/abc',
      cookies: { cypherx_sid: sid },
    });
    expect(res.statusCode).toBe(200);
    expect(t.metricValue('csrf_violations_total')).toBe(0);
  });

  it('login is exempt from CSRF (bootstraps the token)', async () => {
    // login uses its own responder; just assert it is not blocked by CSRF.
    const r = await login(t);
    expect(r.csrf).toBeTruthy();
    expect(t.metricValue('csrf_violations_total')).toBe(0);
  });

  it('PUT and DELETE are also guarded', async () => {
    const { sid, csrf } = await login(t);
    for (const method of ['PUT', 'DELETE'] as const) {
      const res = await t.app.inject({
        method,
        url: '/bff/api/xagent/v1/tasks/abc',
        cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
        // no header -> must be blocked
      });
      expect(res.statusCode).toBe(403);
    }
  });

  it('safeEqual is correct and length-tolerant', () => {
    expect(safeEqual('abc', 'abc')).toBe(true);
    expect(safeEqual('abc', 'abd')).toBe(false);
    expect(safeEqual('abc', 'abcd')).toBe(false);
    expect(safeEqual(undefined, 'abc')).toBe(false);
    expect(safeEqual('abc', undefined)).toBe(false);
  });
});
