import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, type TestApp } from './helpers/testApp.js';
import { login } from './helpers/login.js';
import { DashboardCache, isCacheablePath } from '../src/proxy/cache.js';

describe('dashboard cache (per-tenant, 30s)', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  it('caches an expensive GET and serves the second hit without re-calling upstream', async () => {
    const { sid } = await login(t);
    let upstreamCalls = 0;
    t.upstream.setResponder(() => {
      upstreamCalls += 1;
      return { status: 200, body: `{"total":${upstreamCalls}}` };
    });

    const first = await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/usage',
      cookies: { cypherx_sid: sid },
    });
    const second = await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/usage',
      cookies: { cypherx_sid: sid },
    });

    expect(first.statusCode).toBe(200);
    expect(second.statusCode).toBe(200);
    expect(second.headers['x-bff-cache']).toBe('hit');
    // upstream called only once.
    expect(upstreamCalls).toBe(1);
    expect(second.json()).toEqual({ total: 1 });
  });

  it('does NOT cache a non-expensive GET', async () => {
    const { sid } = await login(t);
    let upstreamCalls = 0;
    t.upstream.setResponder(() => {
      upstreamCalls += 1;
      return { status: 200, body: '{}' };
    });
    await t.app.inject({ method: 'GET', url: '/bff/api/llms/v1/models', cookies: { cypherx_sid: sid } });
    await t.app.inject({ method: 'GET', url: '/bff/api/llms/v1/models', cookies: { cypherx_sid: sid } });
    expect(upstreamCalls).toBe(2);
  });

  it('cache is isolated per tenant (no cross-tenant read)', () => {
    const cache = new DashboardCache(30);
    const body = Buffer.from('A');
    cache.set('tenant-a', 'GET /usage', { status: 200, headers: {}, body });
    expect(cache.get('tenant-a', 'GET /usage')?.body.toString()).toBe('A');
    expect(cache.get('tenant-b', 'GET /usage')).toBeUndefined();
  });

  it('respects TTL expiry', () => {
    let now = 1000;
    const cache = new DashboardCache(30, () => now);
    cache.set('t', 'k', { status: 200, headers: {}, body: Buffer.from('x') });
    expect(cache.get('t', 'k')).toBeDefined();
    now += 31_000;
    expect(cache.get('t', 'k')).toBeUndefined();
  });

  it('isCacheablePath recognises usage/cost/health', () => {
    expect(isCacheablePath('/v1/usage')).toBe(true);
    expect(isCacheablePath('/v1/cost')).toBe(true);
    expect(isCacheablePath('/v1/health')).toBe(true);
    expect(isCacheablePath('/v1/models')).toBe(false);
  });
});
