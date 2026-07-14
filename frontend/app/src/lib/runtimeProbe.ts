/**
 * Probing a sub-agent's xAgent runtime — and telling the FAILURE MODES APART.
 *
 * THE BUG THIS EXISTS TO KILL: the orchestrator page used to do
 *
 *     try   { return await getRuntime(id); }
 *     catch { return null; }              // "no runtime row"
 *
 * which collapsed *every* failure into the single verdict "No runtime". But
 * `GET /v1/agents/{id}/runtime` is gated on the `agent:admin` / `platform:admin` scope, so a
 * session that merely lacks that scope gets a **403** — and the UI reported that as though the
 * agent had never been registered. Two completely different problems, one indistinguishable badge:
 *
 *   * 404  → the agent really has no runtime row. It is invisible to the orchestration roster and
 *            can never be scheduled. **Repair fixes it.**
 *   * 403  → the row may well exist; *we were not allowed to look*. Repair will NOT fix it (the PUT
 *            is gated on the same scope and will 403 too) — the SESSION needs `agent:admin`.
 *   * 5xx / network → we could not check at all. Transient; retry.
 *
 * Reporting a permissions problem as a data problem sends you off repairing agents that were never
 * broken, while the actual cause (a missing scope) stays invisible. So: never guess. Discriminate.
 */
import { BffError } from './bff-client';
import { getRuntime } from './services';
import type { AgentRuntime } from './types';

export type RuntimeProbe =
  /** A runtime row exists and is active — the agent is in the orchestration roster. */
  | { kind: 'ready'; runtime: AgentRuntime }
  /** A runtime row exists but is not active — the roster filters on status='active', so it is skipped. */
  | { kind: 'inactive'; runtime: AgentRuntime }
  /** 404 — no runtime row at all. The roster cannot see this agent. Repairable. */
  | { kind: 'missing' }
  /** 403 — this session may not read agent runtimes. NOT a broken agent; a missing scope. */
  | { kind: 'forbidden'; message: string }
  /** Anything else (5xx, network, timeout). We do not know; do not pretend we do. */
  | { kind: 'error'; status: number; code: string; message: string };

/** True when the orchestration roster will actually see (and be able to schedule) this agent. */
export function isSchedulable(p: RuntimeProbe | undefined): boolean {
  return p?.kind === 'ready';
}

/** True when "Repair" can plausibly help. A 403/5xx is not repairable — it is not about the agent. */
export function isRepairable(p: RuntimeProbe | undefined): boolean {
  return p?.kind === 'missing' || p?.kind === 'inactive';
}

/** The runtime row, when we actually got one. */
export function runtimeOf(p: RuntimeProbe | undefined): AgentRuntime | null {
  return p && (p.kind === 'ready' || p.kind === 'inactive') ? p.runtime : null;
}

/** Classify a runtime row. Exported so the mapping is testable without any network at all. */
export function classifyRuntime(runtime: AgentRuntime): RuntimeProbe {
  return runtime.status === 'active' ? { kind: 'ready', runtime } : { kind: 'inactive', runtime };
}

/**
 * Classify a FAILED runtime read. This is the whole point of the module — see the header.
 * 404 = the agent has no runtime. 403 = we were not allowed to look. They are not the same thing.
 */
export function classifyRuntimeError(err: unknown): RuntimeProbe {
  if (err instanceof BffError) {
    if (err.status === 404) return { kind: 'missing' };
    if (err.status === 403) return { kind: 'forbidden', message: err.message };
    return { kind: 'error', status: err.status, code: err.code, message: err.message };
  }
  return {
    kind: 'error',
    status: 0,
    code: 'NETWORK_ERROR',
    message: err instanceof Error ? err.message : 'Could not reach the agent runtime service.',
  };
}

/** How the runtime is read. Injectable so tests need no module mocking / no network. */
export type RuntimeFetcher = (agentId: string, signal?: AbortSignal) => Promise<AgentRuntime>;

/** Read one sub-agent's runtime, CLASSIFYING the outcome instead of flattening it all to null. */
export async function probeRuntime(
  agentId: string,
  signal?: AbortSignal,
  fetcher: RuntimeFetcher = getRuntime,
): Promise<RuntimeProbe> {
  try {
    return classifyRuntime(await fetcher(agentId, signal));
  } catch (err) {
    return classifyRuntimeError(err);
  }
}
