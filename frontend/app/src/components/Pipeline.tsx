import { cn } from '@/lib/utils';

export type StageState = 'done' | 'active' | 'block' | 'idle';

export interface PipelineStage {
  label: string;
  meta?: string;
  state: StageState;
}

const NODE: Record<StageState, string> = {
  done: 'border-brand bg-brand',
  active: 'border-brand bg-brand cx-ring',
  block: 'border-danger bg-danger',
  idle: 'border-border-2 bg-surface',
};

/** A completed stage passes the flow to the next; active/blocked/idle halt the lit rail. */
function passed(state: StageState): boolean {
  return state === 'done';
}

/**
 * The agent execution pipeline as a horizontal, connected rail of stages
 * (Guard-in → Prompt → LLM → Guard-out → Tools → Memory). State is encoded in
 * form and semantic color; the active node carries a subtle accent ring. Reused on
 * the Overview, the Agent Builder, and Task detail.
 */
export function Pipeline({ stages, className }: { stages: PipelineStage[]; className?: string }) {
  return (
    <div className={cn('flex w-full items-start', className)}>
      {stages.map((s, i) => {
        const leftLit = i > 0 && passed(stages[i - 1].state);
        const rightLit = passed(s.state);
        return (
          <div key={`${s.label}-${i}`} className="flex min-w-0 flex-1 flex-col items-center">
            <div className="flex w-full items-center">
              <span className={cn('h-px flex-1', i === 0 ? 'opacity-0' : leftLit ? 'bg-brand/55' : 'bg-border-2')} />
              <span
                className={cn(
                  'mx-1.5 h-3 w-3 shrink-0 rounded-full border-2',
                  NODE[s.state],
                )}
                aria-hidden="true"
              />
              <span
                className={cn('h-px flex-1', i === stages.length - 1 ? 'opacity-0' : rightLit ? 'bg-brand/55' : 'bg-border-2')}
              />
            </div>
            <span className={cn('mt-1.5 truncate text-center text-[12.5px] font-medium', s.state === 'idle' ? 'text-muted' : 'text-fg')}>
              {s.label}
            </span>
            {s.meta && <span className="mt-0.5 truncate font-mono text-[11px] text-faint">{s.meta}</span>}
          </div>
        );
      })}
    </div>
  );
}
