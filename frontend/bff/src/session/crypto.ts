/**
 * Session encryption-at-rest: AES-256-GCM with a Key-Encryption-Key (KEK) from env.
 *
 * Every session payload is serialised to JSON and sealed before it touches Valkey,
 * so a Valkey compromise (or a leaked RDB snapshot) never exposes downstream tokens
 * or tenant context in plaintext. GCM gives us authenticated encryption — a tampered
 * ciphertext fails to decrypt rather than yielding attacker-controlled session state.
 *
 * On-disk record format (versioned, self-describing):
 *
 *     v1.<iv_b64>.<tag_b64>.<ciphertext_b64>
 *
 * The leading version tag lets us rotate the scheme/algorithm later without guessing.
 */

import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';

const ALGORITHM = 'aes-256-gcm';
const IV_LENGTH = 12; // 96-bit nonce — the recommended size for GCM
const TAG_LENGTH = 16; // 128-bit auth tag
const VERSION = 'v1';

export class SessionCryptoError extends Error {}

/**
 * Stateless sealer/unsealer over a single 32-byte KEK. Construct once at boot and
 * reuse — it holds no per-message state.
 */
export class SessionCrypto {
  private readonly key: Buffer;

  constructor(kek: Buffer) {
    if (kek.length !== 32) {
      throw new SessionCryptoError(`KEK must be 32 bytes for AES-256-GCM (got ${kek.length})`);
    }
    // Copy so an external mutation of the buffer can't change our key mid-flight.
    this.key = Buffer.from(kek);
  }

  /** Seal an arbitrary JSON-serialisable value into the versioned record string. */
  seal(value: unknown): string {
    const plaintext = Buffer.from(JSON.stringify(value), 'utf8');
    const iv = randomBytes(IV_LENGTH);
    const cipher = createCipheriv(ALGORITHM, this.key, iv);
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const tag = cipher.getAuthTag();
    return [
      VERSION,
      iv.toString('base64'),
      tag.toString('base64'),
      ciphertext.toString('base64'),
    ].join('.');
  }

  /** Open a sealed record back into its original value. Throws on any tamper/format error. */
  open<T = unknown>(record: string): T {
    const parts = record.split('.');
    if (parts.length !== 4 || parts[0] !== VERSION) {
      throw new SessionCryptoError('Malformed or unsupported session record');
    }
    const iv = Buffer.from(parts[1] as string, 'base64');
    const tag = Buffer.from(parts[2] as string, 'base64');
    const ciphertext = Buffer.from(parts[3] as string, 'base64');
    if (iv.length !== IV_LENGTH || tag.length !== TAG_LENGTH) {
      throw new SessionCryptoError('Invalid IV or auth tag length');
    }
    try {
      const decipher = createDecipheriv(ALGORITHM, this.key, iv);
      decipher.setAuthTag(tag);
      const plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
      return JSON.parse(plaintext.toString('utf8')) as T;
    } catch (err) {
      // Auth-tag mismatch (tamper / wrong key) or bad JSON — never leak details.
      throw new SessionCryptoError('Failed to decrypt session record', { cause: err });
    }
  }
}
