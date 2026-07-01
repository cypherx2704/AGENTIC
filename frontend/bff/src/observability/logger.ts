/**
 * Structured JSON logging via pino. The BFF handles credentials, so the logger is
 * configured to never serialise cookie/authorization headers or session tokens.
 */
import { pino, type Logger } from 'pino';

export function createLogger(level: string): Logger {
  return pino({
    level,
    // Structured JSON; redact anything that could carry a secret.
    redact: {
      paths: [
        'req.headers.authorization',
        'req.headers.cookie',
        'req.headers["x-csrf-token"]',
        'res.headers["set-cookie"]',
        'downstreamToken',
        'api_key',
        'apiKey',
      ],
      censor: '[REDACTED]',
    },
    formatters: {
      level(label) {
        return { level: label };
      },
    },
  });
}

export type { Logger };
