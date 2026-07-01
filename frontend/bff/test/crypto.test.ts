import { describe, it, expect } from 'vitest';
import { SessionCrypto, SessionCryptoError } from '../src/session/crypto.js';

const KEK = Buffer.alloc(32, 9);

describe('SessionCrypto (AES-256-GCM, encrypted at rest)', () => {
  it('seals and opens a round-trip value', () => {
    const c = new SessionCrypto(KEK);
    const value = { tenantId: 't1', downstreamToken: 'secret.jwt.value', scopes: ['a', 'b'] };
    const sealed = c.seal(value);
    expect(sealed.startsWith('v1.')).toBe(true);
    expect(c.open(sealed)).toEqual(value);
  });

  it('ciphertext does not contain the plaintext secret', () => {
    const c = new SessionCrypto(KEK);
    const sealed = c.seal({ downstreamToken: 'TOP-SECRET-TOKEN-12345' });
    expect(sealed).not.toContain('TOP-SECRET-TOKEN-12345');
  });

  it('produces a different ciphertext each time (random IV)', () => {
    const c = new SessionCrypto(KEK);
    const a = c.seal({ x: 1 });
    const b = c.seal({ x: 1 });
    expect(a).not.toEqual(b);
  });

  it('rejects a tampered ciphertext (auth-tag mismatch)', () => {
    const c = new SessionCrypto(KEK);
    const sealed = c.seal({ x: 1 });
    const parts = sealed.split('.');
    // Flip a byte in the ciphertext.
    const ct = Buffer.from(parts[3]!, 'base64');
    ct[0] = ct[0]! ^ 0xff;
    const tampered = [parts[0], parts[1], parts[2], ct.toString('base64')].join('.');
    expect(() => c.open(tampered)).toThrow(SessionCryptoError);
  });

  it('rejects decryption under a different key', () => {
    const sealed = new SessionCrypto(KEK).seal({ x: 1 });
    const other = new SessionCrypto(Buffer.alloc(32, 1));
    expect(() => other.open(sealed)).toThrow(SessionCryptoError);
  });

  it('rejects a malformed record', () => {
    const c = new SessionCrypto(KEK);
    expect(() => c.open('not-a-record')).toThrow(SessionCryptoError);
    expect(() => c.open('v2.a.b.c')).toThrow(SessionCryptoError);
  });

  it('rejects a wrong-size KEK', () => {
    expect(() => new SessionCrypto(Buffer.alloc(16))).toThrow(SessionCryptoError);
  });
});
