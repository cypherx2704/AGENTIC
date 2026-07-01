import { describe, it, expect, beforeEach } from 'vitest';
import RedisMock from 'ioredis-mock';
import { SessionStore, type RedisLike } from '../src/session/store.js';
import { SessionCrypto } from '../src/session/crypto.js';
import type { SessionData } from '../src/session/types.js';

const KEK = Buffer.alloc(32, 3);

function makeStore(ttlSeconds = 3600): { store: SessionStore; redis: RedisLike } {
  const redis = new RedisMock() as unknown as RedisLike;
  const store = new SessionStore(redis, new SessionCrypto(KEK), {
    keyPrefix: 'test:sess:',
    ttlSeconds,
  });
  return { store, redis };
}

const sampleData: SessionData = {
  tenantId: 'tenant-1',
  agentId: 'agent-1',
  scopes: ['agent:execute', 'llm:invoke'],
  downstreamToken: 'eyJhbGciOi.JWT.payload',
  tokenExpiresAt: Math.floor(Date.now() / 1000) + 3600,
  csrfToken: 'csrf-abc',
  createdAt: Date.now(),
};

describe('SessionStore lifecycle', () => {
  let store: SessionStore;
  let redis: RedisLike;

  beforeEach(() => {
    ({ store, redis } = makeStore());
  });

  it('create returns an opaque id and read round-trips the data', async () => {
    const sid = await store.create(sampleData);
    expect(typeof sid).toBe('string');
    expect(sid.length).toBeGreaterThan(20);
    const loaded = await store.read(sid);
    expect(loaded).not.toBeNull();
    expect(loaded!.data).toEqual(sampleData);
  });

  it('stores the record ENCRYPTED at rest (raw value has no plaintext token)', async () => {
    const sid = await store.create(sampleData);
    const raw = await redis.get(`test:sess:${sid}`);
    expect(raw).not.toBeNull();
    expect(raw!).not.toContain(sampleData.downstreamToken);
    expect(raw!).not.toContain('tenant-1');
    expect(raw!.startsWith('v1.')).toBe(true);
  });

  it('read of an unknown id returns null', async () => {
    expect(await store.read('does-not-exist')).toBeNull();
  });

  it('destroy (logout) removes the session', async () => {
    const sid = await store.create(sampleData);
    await store.destroy(sid);
    expect(await store.read(sid)).toBeNull();
    expect(await redis.get(`test:sess:${sid}`)).toBeNull();
  });

  it('expired (deleted) session reads as null', async () => {
    const sid = await store.create(sampleData);
    // Simulate TTL expiry by deleting the key directly.
    await redis.del(`test:sess:${sid}`);
    expect(await store.read(sid)).toBeNull();
  });

  it('a corrupted/undecryptable record reads as null and is purged', async () => {
    const { store: s2, redis: r2 } = makeStore();
    const sid = await s2.create(sampleData);
    // Corrupt the stored record under a DIFFERENT key scheme.
    await r2.set('test:sess:' + sid, 'v1.garbage.garbage.garbage', 'EX', 60);
    expect(await s2.read(sid)).toBeNull();
    expect(await r2.get('test:sess:' + sid)).toBeNull();
  });

  it('update overwrites in place keeping the same id', async () => {
    const sid = await store.create(sampleData);
    const updated: SessionData = { ...sampleData, scopes: ['only:one'] };
    await store.update(sid, updated);
    const loaded = await store.read(sid);
    expect(loaded!.data.scopes).toEqual(['only:one']);
  });
});
