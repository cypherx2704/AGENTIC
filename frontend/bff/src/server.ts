/**
 * Production entrypoint. Loads config (fail-fast), connects a real ioredis client to
 * Valkey, wires the global fetch, builds the app, and listens. Handles graceful
 * shutdown on SIGINT/SIGTERM so in-flight requests drain and Valkey closes cleanly.
 */
import Redis from 'ioredis';
import { loadConfig } from './config/index.js';
import { buildApp } from './app.js';
import { createLogger } from './observability/logger.js';
import type { FetchLike, UpstreamResponse } from './context.js';
import type { RedisLike } from './session/store.js';
import type { ReadinessProbe } from './routes/health.js';

/** Adapt the global WHATWG fetch to our injectable FetchLike signature. */
const nodeFetch: FetchLike = (input, init) =>
  fetch(input, init as RequestInit) as unknown as Promise<UpstreamResponse>;

async function main(): Promise<void> {
  const config = loadConfig();
  const log = createLogger(config.logLevel);

  const redis = new Redis(config.valkeyUrl, {
    lazyConnect: false,
    maxRetriesPerRequest: 2,
    enableReadyCheck: true,
  });
  redis.on('error', (err) => log.error({ err: err.message }, 'valkey error'));

  const readiness: ReadinessProbe = {
    async ping() {
      const pong = await redis.ping();
      return pong === 'PONG';
    },
  };

  const app = await buildApp({
    config,
    redis: redis as unknown as RedisLike,
    fetch: nodeFetch,
    readiness,
    logger: log,
  });

  const shutdown = async (signal: string): Promise<void> => {
    log.info({ signal }, 'shutting down');
    try {
      await app.close();
      redis.disconnect();
    } finally {
      process.exit(0);
    }
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));

  await app.listen({ host: config.host, port: config.port });
  log.info(
    { host: config.host, port: config.port, upstreams: Object.keys(config.upstreams) },
    'CypherX BFF listening',
  );
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error('BFF failed to start:', err);
  process.exit(1);
});
