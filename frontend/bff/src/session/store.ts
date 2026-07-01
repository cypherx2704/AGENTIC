/**
 * Server-side session store backed by Valkey (Redis protocol).
 *
 * Responsibilities:
 *   - mint cryptographically-random opaque session ids (the cookie value)
 *   - seal session data with AES-256-GCM before writing (encrypted at rest)
 *   - apply a sliding idle TTL (refreshed on every authenticated read)
 *   - hard-expire + delete on logout
 *
 * The store depends only on a tiny structural {@link RedisLike} interface so it
 * works identically against the real `ioredis` client and an in-memory
 * `ioredis-mock` in tests — no live infra needed for the test suite.
 */

import { randomBytes } from 'node:crypto';
import { SessionCrypto } from './crypto.js';
import type { LoadedSession, SessionData } from './types.js';

/** The subset of the Redis client surface the store actually uses. */
export interface RedisLike {
  set(
    key: string,
    value: string,
    mode: 'EX',
    ttlSeconds: number,
  ): Promise<unknown>;
  get(key: string): Promise<string | null>;
  del(key: string): Promise<unknown>;
  expire(key: string, ttlSeconds: number): Promise<unknown>;
}

export interface SessionStoreOptions {
  readonly keyPrefix: string;
  readonly ttlSeconds: number;
}

/** Generate an opaque, URL-safe 256-bit session id. */
export function generateSessionId(): string {
  return randomBytes(32).toString('base64url');
}

/** Generate a 256-bit CSRF token. */
export function generateCsrfToken(): string {
  return randomBytes(32).toString('base64url');
}

export class SessionStore {
  constructor(
    private readonly redis: RedisLike,
    private readonly crypto: SessionCrypto,
    private readonly opts: SessionStoreOptions,
  ) {}

  private redisKey(sid: string): string {
    return `${this.opts.keyPrefix}${sid}`;
  }

  /**
   * Create a new session: mint an id, seal the data, write with the idle TTL.
   * Returns the new (opaque) session id to set as the cookie.
   */
  async create(data: SessionData): Promise<string> {
    const sid = generateSessionId();
    const sealed = this.crypto.seal(data);
    await this.redis.set(this.redisKey(sid), sealed, 'EX', this.opts.ttlSeconds);
    return sid;
  }

  /**
   * Read + decrypt a session by id. Returns null when the id is unknown/expired or
   * when the record fails to decrypt (tamper / KEK mismatch — treated as no session).
   * On a successful read the idle TTL is refreshed (sliding window).
   */
  async read(sid: string): Promise<LoadedSession | null> {
    if (!sid) return null;
    const key = this.redisKey(sid);
    const sealed = await this.redis.get(key);
    if (sealed === null) return null;
    let data: SessionData;
    try {
      data = this.crypto.open<SessionData>(sealed);
    } catch {
      // Undecryptable record — drop it and behave as if there's no session.
      await this.redis.del(key);
      return null;
    }
    // Sliding refresh of the idle TTL.
    await this.redis.expire(key, this.opts.ttlSeconds);
    return { sid, data };
  }

  /** Overwrite an existing session's data in place, keeping the same id + sliding TTL. */
  async update(sid: string, data: SessionData): Promise<void> {
    const sealed = this.crypto.seal(data);
    await this.redis.set(this.redisKey(sid), sealed, 'EX', this.opts.ttlSeconds);
  }

  /** Destroy a session (logout). Idempotent. */
  async destroy(sid: string): Promise<void> {
    if (!sid) return;
    await this.redis.del(this.redisKey(sid));
  }
}
