import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { makeTestApp, type TestApp } from './helpers/testApp.js';
import { login, DOWNSTREAM_TOKEN } from './helpers/login.js';

/** Build an async-iterable of SSE chunks. */
async function* sseChunks(events: string[]): AsyncIterable<Uint8Array> {
  const enc = new TextEncoder();
  for (const e of events) {
    yield enc.encode(e);
  }
}

describe('SSE stream relay for xagent task stream', () => {
  let t: TestApp;

  beforeEach(async () => {
    t = await makeTestApp();
  });
  afterEach(async () => {
    await t.app.close();
  });

  it('relays the upstream SSE stream with text/event-stream and injects identity', async () => {
    const { sid } = await login(t, { tenantId: 'tenant-stream' });
    t.upstream.setResponder(() => ({
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
      stream: sseChunks([
        'event: step\ndata: {"step":"llm_call"}\n\n',
        'event: done\ndata: {"status":"completed"}\n\n',
      ]),
    }));

    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/xagent/v1/tasks/task-123/stream',
      cookies: { cypherx_sid: sid },
    });

    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toContain('text/event-stream');
    expect(res.body).toContain('event: step');
    expect(res.body).toContain('"status":"completed"');

    // Identity was injected on the upstream stream request.
    const call = t.upstream.lastCall()!;
    expect(call.url).toBe('http://xagent.test/v1/tasks/task-123/stream');
    expect(call.headers['authorization']).toBe(`Bearer ${DOWNSTREAM_TOKEN}`);
    expect(call.headers['x-tenant-id']).toBe('tenant-stream');
    // The relayed stream never carries the token to the browser.
    expect(res.body).not.toContain(DOWNSTREAM_TOKEN);
  });

  it('requires a session for the stream route', async () => {
    const res = await t.app.inject({
      method: 'GET',
      url: '/bff/api/xagent/v1/tasks/task-123/stream',
    });
    expect(res.statusCode).toBe(401);
  });
});
