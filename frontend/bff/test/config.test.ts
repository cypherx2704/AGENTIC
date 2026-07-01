import { describe, it, expect } from 'vitest';
import { loadConfig, ConfigError } from '../src/config/index.js';
import { TEST_KEK_B64, testEnv } from './helpers/testApp.js';

describe('config loading (fail-fast, env-driven)', () => {
  it('loads a valid config', () => {
    const c = loadConfig(testEnv());
    expect(c.upstreams.auth).toBe('http://auth.test');
    expect(c.sessionKek.length).toBe(32);
    expect(c.cookie.sessionName).toBe('cypherx_sid');
  });

  it('throws when VALKEY_URL is missing', () => {
    const env = testEnv();
    delete (env as Record<string, string | undefined>).VALKEY_URL;
    expect(() => loadConfig(env)).toThrow(ConfigError);
  });

  it('throws when AUTH_URL is missing (login needs it)', () => {
    const env = testEnv();
    delete (env as Record<string, string | undefined>).AUTH_URL;
    expect(() => loadConfig(env)).toThrow(/AUTH_URL/);
  });

  it('throws when the KEK is not 32 bytes', () => {
    const env = testEnv({ SESSION_KEK_BASE64: Buffer.alloc(16).toString('base64') });
    expect(() => loadConfig(env)).toThrow(/32 bytes/);
  });

  it('throws when KEK is absent', () => {
    const env = testEnv();
    delete (env as Record<string, string | undefined>).SESSION_KEK_BASE64;
    expect(() => loadConfig(env)).toThrow(ConfigError);
  });

  it('rejects SameSite=None without Secure', () => {
    expect(() => loadConfig(testEnv({ COOKIE_SAMESITE: 'none', COOKIE_SECURE: 'false' }))).toThrow(
      /requires COOKIE_SECURE/,
    );
  });

  it('strips trailing slashes from upstream URLs', () => {
    const c = loadConfig(testEnv({ AUTH_URL: 'http://auth.test/' }));
    expect(c.upstreams.auth).toBe('http://auth.test');
  });

  it('omits optional upstreams that are not configured', () => {
    const env = testEnv();
    delete (env as Record<string, string | undefined>).RAG_URL;
    const c = loadConfig(env);
    expect(c.upstreams.rag).toBeUndefined();
    expect(c.upstreams.auth).toBeDefined();
  });

  it('uses TEST_KEK_B64 that decodes to 32 bytes', () => {
    expect(Buffer.from(TEST_KEK_B64, 'base64').length).toBe(32);
  });
});
