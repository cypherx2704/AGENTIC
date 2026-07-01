'use client';

import { StatusBadge } from '@/components/ui';
import type { TaskStep } from '@/lib/types';
import { formatDuration, formatNumber } from '@/lib/utils';

/** Human label for the canonical xAgent step names. */
const STEP_LABELS: Record<string, string> = {
  guardrail_check_input: 'Guardrail (input)',
  load: 'Load agent config',
  prompt_build: 'Build prompt',
  rag_query: 'RAG retrieval',
  memory_retrieve: 'Memory retrieve',
  memory_write: 'Memory write',
  llm_call: 'LLM call',
  tool_loop: 'Tool loop',
  guardrail_check_output: 'Guardrail (output)',
  event: 'Emit event',
};

function label(step: string): string {
  return STEP_LABELS[step] ?? step;
}

/**
 * Ordered step/stage timeline (guardrail-in → llm → guardrail-out) with per-step
 * status, duration and tokens. Shared by the Task Runner and Task Detail screens.
 */
export function TaskTimeline({ steps }: { steps: TaskStep[] }) {
  if (steps.length === 0) {
    return <p className="py-6 text-center text-sm text-muted">No steps recorded yet.</p>;
  }

  return (
    <ol className="relative ml-3 border-l border-border">
      {steps.map((s, i) => {
        const ok = ['passed', 'completed', 'ok', 'allow', 'redacted'].includes((s.status ?? '').toLowerCase());
        const failed = ['failed', 'blocked', 'error'].includes((s.status ?? '').toLowerCase());
        const dot = failed ? 'bg-danger' : ok ? 'bg-success' : 'bg-warning';
        return (
          <li key={`${s.step}-${i}`} className="mb-4 ml-4">
            <span className={`absolute -left-[7px] mt-1.5 h-3 w-3 rounded-full border-2 border-bg ${dot}`} />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-fg">{label(s.step)}</span>
                <StatusBadge status={s.status} />
              </div>
              <div className="flex items-center gap-3 font-mono text-xs text-muted">
                <span title="duration">{formatDuration(s.duration_ms ?? null)}</span>
                <span title="tokens">{s.tokens != null ? `${formatNumber(s.tokens)} tok` : '—'}</span>
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
