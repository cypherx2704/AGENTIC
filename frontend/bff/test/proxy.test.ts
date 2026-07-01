import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, parseSetCookies, type TestApp } from './helpers/testApp.js';
import { login, DOWNSTREAM_TOKEN } from './helpers/login.js';
import { parseProxyPath } from '../src/proxy/index.js';

describe('downstream proxy: header injection, key custody, routing', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  it('requires a session (401 when unauthenticated)', async () => {
    const res = await t.app.inject({ method: 'GET', url: '/bff/api/llms/v1/models' });
    expect(res.statusCode).toBe(401);
  });

  it('drops a session whose downstream token has already expired (401 at the boundary, no upstream call)', async () => {
    // Mint a session whose agent JWT is already past its absolute expiry.
    t.upstream.setResponder(() => ({
      status: 200,
      body: JSON.stringify({
        user_id: 'user-1',
        tenant_id: 'tenant-exp',
        agent_id: 'orch-1',
        token: DOWNSTREAM_TOKEN,
        token_type: 'Bearer',
        expires_in: -100,
        scopes: ['llm:invoke'],
      }),
    }));
    const loginRes = await t.app.inject({
      method: 'POST',
      url: '/bff/login',
      payload: { email: 'user@example.com', password: 'hunter2pw' },
    });
    expect(loginRes.statusCode).toBe(200);
    const sid = parseSetCookies(loginRes.headers['set-cookie'])['cypherx_sid']!.value;

    // The expired session must be rejected at the boundary — never forwarded upstream.
    let upstreamCalls = 0;
    t.upstream.setResponder(() => {
      upstreamCalls++;
      return { status: 200, body: '{}' };
    });
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/models',
      cookies: { cypherx_sid: sid },
    });
    expect(res.statusCode).toBe(401);
    expect(upstreamCalls).toBe(0);

    // …and /bff/me now reports unauthenticated (the stale record was destroyed).
    const me = await t.app.inject({ method: 'GET', url: '/bff/me', cookies: { cypherx_sid: sid } });
    expect(me.statusCode).toBe(401);
  });

  it('injects Authorization (session token), X-Tenant-ID, X-Request-ID, traceparent downstream', async () => {
    const { sid } = await login(t, { tenantId: 'tenant-77' });
    t.upstream.setResponder(() => ({ status: 200, body: '{"models":[]}' }));

    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/models',
      cookies: { cypherx_sid: sid },
    });
    expect(res.statusCode).toBe(200);

    const call = t.upstream.lastCall()!;
    expect(call.url).toBe('http://llms.test/v1/models');
    expect(call.headers['authorization']).toBe(`Bearer ${DOWNSTREAM_TOKEN}`);
    expect(call.headers['x-tenant-id']).toBe('tenant-77');
    expect(call.headers['x-request-id']).toBeTruthy();
    expect(call.headers['traceparent']).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/);
  });

  it('KEY CUSTODY: the downstream token never reaches the browser response', async () => {
    const { sid } = await login(t);
    // Even if a malicious/buggy upstream echoes the token back, the body is opaque to
    // us; assert our own injected headers/cookies do not leak it. Here upstream returns
    // a benign body.
    t.upstream.setResponder(() => ({ status: 200, body: '{"ok":true}' }));
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/models',
      cookies: { cypherx_sid: sid },
    });
    expect(res.body).not.toContain(DOWNSTREAM_TOKEN);
    expect(JSON.stringify(res.headers)).not.toContain(DOWNSTREAM_TOKEN);
  });

  it('strips client-supplied Authorization / X-Tenant-ID (no spoofing)', async () => {
    const { sid } = await login(t, { tenantId: 'real-tenant' });
    t.upstream.setResponder(() => ({ status: 200, body: '{}' }));
    await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/models',
      cookies: { cypherx_sid: sid },
      headers: {
        authorization: 'Bearer ATTACKER-TOKEN',
        'x-tenant-id': 'victim-tenant',
        'x-forwarded-agent-jwt': 'evil',
      },
    });
    const call = t.upstream.lastCall()!;
    expect(call.headers['authorization']).toBe(`Bearer ${DOWNSTREAM_TOKEN}`);
    expect(call.headers['authorization']).not.toContain('ATTACKER-TOKEN');
    expect(call.headers['x-tenant-id']).toBe('real-tenant');
    expect(call.headers['x-forwarded-agent-jwt']).toBeUndefined();
  });

  it('strips hop-by-hop headers from the inbound request', async () => {
    const { sid } = await login(t);
    t.upstream.setResponder(() => ({ status: 200, body: '{}' }));
    await t.app.inject({
      method: 'GET',
      url: '/bff/api/llms/v1/models',
      cookies: { cypherx_sid: sid },
      headers: { connection: 'keep-alive', te: 'trailers', upgrade: 'websocket' },
    });
    const call = t.upstream.lastCall()!;
    expect(call.headers['connection']).toBeUndefined();
    expect(call.headers['te']).toBeUndefined();
    expect(call.headers['upgrade']).toBeUndefined();
    // The browser's Cookie header must not be forwarded upstream.
    expect(call.headers['cookie']).toBeUndefined();
  });

  it('routes each prefix to its configured upstream', async () => {
    const { sid, csrf } = await login(t);
    const cases: Array<[string, string]> = [
      ['/bff/api/auth/v1/agents', 'http://auth.test/v1/agents'],
      ['/bff/api/guardrails/v1/policies', 'http://guardrails.test/v1/policies'],
      ['/bff/api/rag/v1/kb', 'http://rag.test/v1/kb'],
    ];
    for (const [path, expectedUrl] of cases) {
      t.upstream.setResponder(() => ({ status: 200, body: '{}' }));
      const res = await t.app.inject({
        method: 'POST',
        url: path,
        cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
        headers: { 'x-csrf-token': csrf, 'content-type': 'application/json' },
        payload: { a: 1 },
      });
      expect(res.statusCode).toBe(200);
      expect(t.upstream.lastCall()!.url).toBe(expectedUrl);
    }
  });

  it('unknown upstream prefix -> 404', async () => {
    const { sid } = await login(t);
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/nope/v1/thing',
      cookies: { cypherx_sid: sid },
    });
    expect(res.statusCode).toBe(404);
  });

  it('forwards the request body and query string', async () => {
    const { sid, csrf } = await login(t);
    t.upstream.setResponder(() => ({ status: 201, body: '{"id":"x"}' }));
    const res = await t.app.inject({
      method: 'POST',
      url: '/bff/api/xagent/v1/tasks?foo=bar',
      cookies: { cypherx_sid: sid, cypherx_csrf: csrf },
      headers: { 'x-csrf-token': csrf, 'content-type': 'application/json' },
      payload: { input: { message: 'hello' } },
    });
    expect(res.statusCode).toBe(201);
    const call = t.upstream.lastCall()!;
    expect(call.url).toBe('http://xagent.test/v1/tasks?foo=bar');
    expect(JSON.parse(call.body!)).toEqual({ input: { message: 'hello' } });
  });

  it('relays the upstream status and body back to the client', async () => {
    const { sid } = await login(t);
    t.upstream.setResponder(() => ({ status: 422, body: '{"error":"blocked"}' }));
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/xagent/v1/tasks/abc',
      cookies: { cypherx_sid: sid },
    });
    expect(res.statusCode).toBe(422);
    expect(res.json()).toEqual({ error: 'blocked' });
  });

  it('returns 502 when the upstream throws', async () => {
    const { sid } = await login(t);
    t.upstream.setResponder(() => {
      throw new Error('connection refused');
    });
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/xagent/v1/tasks/abc',
      cookies: { cypherx_sid: sid },
    });
    expect(res.statusCode).toBe(502);
  });

  it('parseProxyPath handles service/rest splitting', () => {
    expect(parseProxyPath('/bff/api/llms/v1/models')).toEqual({ service: 'llms', rest: '/v1/models' });
    expect(parseProxyPath('/bff/api/xagent')).toEqual({ service: 'xagent', rest: '' });
    expect(parseProxyPath('/bff/api/')).toBeNull();
    expect(parseProxyPath('/other')).toBeNull();
  });
});
