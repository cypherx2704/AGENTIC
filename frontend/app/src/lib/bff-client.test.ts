import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BffError, api, bffFetch, fetchSession, login, setCsrfToken, setUnauthorizedHandler } from './bff-client';

function mockResponse(status: number, body: unknown, ok = status >= 200 && status < 300): Response {
  return {
    ok,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  } as unknown as Response;
}

describe('bffFetch', () => {
  beforeEach(() => {
    setCsrfToken(null);
    document.cookie = '';
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sends credentials: include and parses JSON on success', async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, { hello: 'world' }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await bffFetch<{ hello: string }>('/me');
    expect(result).toEqual({ hello: 'world' });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe('include');
    expect(init.method).toBe('GET');
  });

  it('does NOT send the CSRF header on GET', async () => {
    setCsrfToken('csrf-abc');
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal('fetch', fetchMock);

    await bffFetch('/me');
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers['X-CSRF-Token']).toBeUndefined();
  });

  it('echoes the cached CSRF token on a POST mutation', async () => {
    setCsrfToken('csrf-xyz');
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal('fetch', fetchMock);

    await bffFetch('/login', { method: 'POST', body: { a: 1 } });
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers['X-CSRF-Token']).toBe('csrf-xyz');
    expect(headers['Content-Type']).toBe('application/json');
  });

  it('throws a BffError carrying the Contract-2 envelope on error', async () => {
    const envelope = {
      error: { code: 'GUARDRAIL_VIOLATION', message: 'blocked', trace_id: 't-1', request_id: 'r-1' },
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(422, envelope)));

    await expect(bffFetch('/api/xagent/v1/tasks', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 422,
      code: 'GUARDRAIL_VIOLATION',
      traceId: 't-1',
    });
  });

  it('marks a 422 GUARDRAIL_VIOLATION as a guardrail violation', async () => {
    const err = new BffError(422, { code: 'GUARDRAIL_VIOLATION', message: 'x' });
    expect(err.isGuardrailViolation).toBe(true);
    expect(err.isUnauthorized).toBe(false);
  });

  it('marks a 401 as unauthorized', async () => {
    const err = new BffError(401, { code: 'UNAUTHORIZED', message: 'x' });
    expect(err.isUnauthorized).toBe(true);
  });

  it('synthesizes an envelope for a non-conforming error body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(500, 'gateway exploded' as unknown)));
    await expect(bffFetch('/me')).rejects.toMatchObject({ status: 500, code: 'SERVICE_UNAVAILABLE' });
  });

  it('wraps a network failure as a SERVICE_UNAVAILABLE BffError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(bffFetch('/me')).rejects.toMatchObject({ status: 0, code: 'SERVICE_UNAVAILABLE' });
  });

  it('returns undefined on 204 No Content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(204, undefined)));
    await expect(bffFetch('/api/auth/v1/agents/x/keys/y', { method: 'DELETE' })).resolves.toBeUndefined();
  });
});

describe('global 401 interceptor', () => {
  const envelope = { error: { code: 'UNAUTHENTICATED', message: 'No active session' } };

  beforeEach(() => setCsrfToken(null));
  afterEach(() => setUnauthorizedHandler(null));

  it('fires the unauthorized handler when a proxied call returns 401', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(401, envelope)));

    await expect(api('llms', '/v1/models')).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('does NOT fire the handler on a 401 from the /me session probe', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(401, envelope)));

    await expect(fetchSession()).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
  });

  it('does NOT fire the handler on a 401 from /login (credential failure shown inline)', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(mockResponse(401, { error: { code: 'INVALID_CREDENTIALS', message: 'bad' } })),
    );

    await expect(bffFetch('/login', { method: 'POST', body: {} })).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('api() proxy helper', () => {
  it('builds the /api/<service>/<path> URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, { ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    await api('xagent', '/v1/tasks');
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('/bff/api/xagent/v1/tasks');
  });

  it('appends query params, skipping null/undefined/empty', async () => {
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal('fetch', fetchMock);
    await api('xagent', '/v1/tasks', { query: { status: 'running', agent_id: null, since: undefined, limit: 50 } });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('status=running');
    expect(url).toContain('limit=50');
    expect(url).not.toContain('agent_id');
    expect(url).not.toContain('since');
  });
});

describe('session helpers', () => {
  beforeEach(() => setCsrfToken(null));

  it('fetchSession caches the csrf token from /bff/me', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        mockResponse(200, { authenticated: true, tenant_id: 't', scopes: [], csrf_token: 'cached-csrf' }),
      ),
    );
    const session = await fetchSession();
    expect(session.csrf_token).toBe('cached-csrf');

    // A subsequent mutation should now carry that token.
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal('fetch', fetchMock);
    await bffFetch('/logout', { method: 'POST' });
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers['X-CSRF-Token']).toBe('cached-csrf');
  });

  it('login posts email + password then re-reads the session', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(mockResponse(200, {})) // POST /login
      .mockResolvedValueOnce(
        mockResponse(200, { authenticated: true, tenant_id: 't1', scopes: ['platform:admin'], csrf_token: 'c' }),
      ); // GET /me
    vi.stubGlobal('fetch', fetchMock);

    const session = await login('user@example.com', 'hunter2pw');
    expect(session.authenticated).toBe(true);
    expect(session.tenant_id).toBe('t1');

    const loginBody = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(loginBody).toEqual({ email: 'user@example.com', password: 'hunter2pw' });
  });
});
