import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, type TestApp } from './helpers/testApp.js';

describe('operational endpoints', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  it('GET /livez -> 200 ok', async () => {
    const res = await t.app.inject({ method: 'GET', url: '/livez' });
    expect(res.statusCode).toBe(200);
    expect(res.json().status).toBe('ok');
  });

  it('GET /readyz -> 200 when Valkey reachable', async () => {
    const res = await t.app.inject({ method: 'GET', url: '/readyz' });
    expect(res.statusCode).toBe(200);
    expect(res.json().checks.valkey).toBe('ok');
  });

  it('GET /readyz -> 503 when Valkey unreachable', async () => {
    t.ready.value = false;
    const res = await t.app.inject({ method: 'GET', url: '/readyz' });
    expect(res.statusCode).toBe(503);
  });

  it('GET /metrics exposes csrf_violations_total in Prometheus format', async () => {
    const res = await t.app.inject({ method: 'GET', url: '/metrics' });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toContain('text/plain');
    expect(res.body).toContain('# TYPE csrf_violations_total counter');
    expect(res.body).toContain('csrf_violations_total');
  });

  it('GET /metrics counts http requests', async () => {
    await t.app.inject({ method: 'GET', url: '/livez' });
    const res = await t.app.inject({ method: 'GET', url: '/metrics' });
    expect(res.body).toContain('bff_http_requests_total');
  });
});
