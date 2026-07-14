import { describe, expect, it } from 'vitest';
import { BffError } from './bff-client';
import {
  classifyRuntime,
  classifyRuntimeError,
  isRepairable,
  isSchedulable,
  probeRuntime,
  runtimeOf,
} from './runtimeProbe';
import type { AgentRuntime } from './types';

function runtime(over: Partial<AgentRuntime> = {}): AgentRuntime {
  return { agent_id: 'a', name: 'wiki', status: 'active', allowed_tools: [], ...over } as AgentRuntime;
}

function bff(status: number, code = 'X', message = 'boom') {
  return new BffError(status, { code, message } as never);
}

describe('classifyRuntime', () => {
  it('an ACTIVE runtime is the only state the orchestration roster can schedule', () => {
    const p = classifyRuntime(runtime());
    expect(p.kind).toBe('ready');
    expect(isSchedulable(p)).toBe(true);
  });

  it('a non-active runtime is inactive — the roster filters on active, so it is skipped', () => {
    const p = classifyRuntime(runtime({ status: 'inactive' }));
    expect(p.kind).toBe('inactive');
    expect(isSchedulable(p)).toBe(false);
    expect(isRepairable(p)).toBe(true); // reactivating IS the fix here
  });
});

describe('classifyRuntimeError — the bug this module exists to kill', () => {
  it('404 means the agent genuinely has NO runtime row, and Repair is the fix', () => {
    const p = classifyRuntimeError(bff(404, 'NOT_FOUND'));
    expect(p.kind).toBe('missing');
    expect(isRepairable(p)).toBe(true);
  });

  it('403 is NOT reported as "no runtime" — it is a missing SCOPE, not a missing agent', () => {
    // GET /v1/agents/{id}/runtime is gated on agent:admin. The old code caught EVERY error and
    // returned null, so a session merely lacking that scope made every agent look unregistered —
    // sending the operator off "repairing" agents that were never broken, while the real cause
    // (the scope) stayed invisible.
    const p = classifyRuntimeError(bff(403, 'FORBIDDEN', 'requires agent:admin'));

    expect(p.kind).toBe('forbidden');
    expect(p.kind).not.toBe('missing');
    // ...and Repair must NOT be offered: the PUT is gated on the SAME scope and would 403 too.
    expect(isRepairable(p)).toBe(false);
    expect(isSchedulable(p)).toBe(false);
  });

  it('500 is NOT reported as "no runtime" — we simply could not check', () => {
    const p = classifyRuntimeError(bff(500, 'INTERNAL_ERROR'));
    expect(p).toMatchObject({ kind: 'error', status: 500, code: 'INTERNAL_ERROR' });
    expect(isRepairable(p)).toBe(false);
  });

  it('a network failure is an error, not a missing agent', () => {
    const p = classifyRuntimeError(new TypeError('Failed to fetch'));
    expect(p).toMatchObject({ kind: 'error', code: 'NETWORK_ERROR' });
    expect(isRepairable(p)).toBe(false);
  });
});

describe('runtimeOf', () => {
  it('yields the row only when one was actually read', () => {
    expect(runtimeOf(classifyRuntime(runtime({ description: 'look things up' })))?.description).toBe(
      'look things up',
    );
    expect(runtimeOf(classifyRuntimeError(bff(403)))).toBeNull();
    expect(runtimeOf(classifyRuntimeError(bff(404)))).toBeNull();
    expect(runtimeOf(undefined)).toBeNull();
  });
});

describe('probeRuntime', () => {
  it('classifies a successful read', async () => {
    const p = await probeRuntime('a', undefined, async () => runtime({ description: 'd' }));
    expect(p.kind).toBe('ready');
    expect(runtimeOf(p)?.description).toBe('d');
  });

  it('never throws — a rejected read becomes a classified probe', async () => {
    const p = await probeRuntime('a', undefined, async () => {
      throw bff(403, 'FORBIDDEN', 'requires agent:admin');
    });
    expect(p.kind).toBe('forbidden');
  });
});
