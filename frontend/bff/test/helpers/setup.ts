/**
 * Vitest global setup. The suite builds many short-lived Fastify instances (one per
 * `makeTestApp`), each of which transiently registers listeners on shared Node
 * EventEmitters. That trips Node's default 10-listener leak heuristic with a noisy
 * (but harmless) warning. Raise the ceiling for the test process only — production
 * (server.ts) builds exactly one app and registers two signal handlers.
 */
import { setMaxListeners } from 'node:events';

setMaxListeners(100);
