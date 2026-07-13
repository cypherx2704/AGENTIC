/**
 * Shared mapping from xAgent task steps to the horizontal execution-pipeline rail
 * (<Pipeline>). Used by the Overview, Task Runner, and Task Detail so a task's stages
 * read identically everywhere.
 */
import type { PipelineStage, StageState } from '@/components/Pipeline';
import type { TaskStep } from './types';

const STEP_LABEL: Record<string, string> = {
  guardrail_check_input: 'Guard In',
  input: 'Guard In',
  pre_guardrail: 'Guard In',
  guard_in: 'Guard In',
  load: 'Load',
  prompt_build: 'Prompt',
  prompt: 'Prompt',
  rag_query: 'Retrieve',
  memory_retrieve: 'Memory',
  llm_call: 'LLM',
  llm: 'LLM',
  tool_loop: 'Tools',
  tool_call: 'Tools',
  tools: 'Tools',
  guardrail_check_output: 'Guard Out',
  post_guardrail: 'Guard Out',
  guard_out: 'Guard Out',
  output: 'Guard Out',
  memory_write: 'Memory',
  memory: 'Memory',
  event: 'Event',
};

export function stageLabel(step: string): string {
  const key = (step ?? '').toLowerCase();
  return STEP_LABEL[key] ?? (step ?? '—').replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function stepState(status: string): StageState {
  const s = (status ?? '').toLowerCase();
  if (['completed', 'ok', 'allow', 'success', 'done', 'passed', 'redacted'].includes(s)) return 'done';
  if (['running', 'in_progress', 'active', 'started'].includes(s)) return 'active';
  if (['blocked', 'failed', 'error', 'timeout', 'block', 'denied'].includes(s)) return 'block';
  return 'idle';
}

export function stepsToStages(steps: TaskStep[] | undefined): PipelineStage[] {
  if (!steps || steps.length === 0) return [];
  return steps.map((st) => ({
    label: stageLabel(st.step),
    meta: st.duration_ms != null ? `${st.duration_ms}ms` : st.tokens != null ? `${st.tokens} tok` : undefined,
    state: stepState(st.status),
  }));
}

/** The canonical idle rail shown before any steps exist. */
export const CANONICAL_STAGES: PipelineStage[] = ['Guard In', 'Prompt', 'LLM', 'Guard Out', 'Tools', 'Memory'].map(
  (label) => ({ label, state: 'idle' as StageState }),
);
