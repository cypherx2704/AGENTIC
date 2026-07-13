import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

type Tone = 'info' | 'success' | 'warning' | 'danger' | 'neutral';

const tones: Record<Tone, string> = {
  neutral: 'border-border bg-surface-2 text-muted',
  info: 'border-brand/30 bg-brand/10 text-fg',
  success: 'border-success/30 bg-success/10 text-fg',
  warning: 'border-warning/40 bg-warning/10 text-fg',
  danger: 'border-danger/40 bg-danger/10 text-fg',
};

const titleTones: Record<Tone, string> = {
  neutral: 'text-fg',
  info: 'text-brand',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
};

/**
 * A semantic inline banner for a single message (info/success/warning/danger). Replaces the
 * hand-rolled callout divs the design audit flagged — styles through tokens only. Use for
 * "shown once" notices, danger-zone warnings, empty-with-context states, and result summaries.
 */
export function Callout({
  tone = 'info',
  title,
  children,
  className,
  actions,
}: {
  tone?: Tone;
  /** Optional bold lead line (Title-Cased chrome); omit for a plain sentence-case notice. */
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
  /** Optional trailing controls (e.g. a dismiss or retry Button), right-aligned. */
  actions?: ReactNode;
}) {
  return (
    <div className={cn('flex items-start gap-3 rounded-md border px-3 py-2.5 text-sm', tones[tone], className)}>
      <div className="min-w-0 flex-1">
        {title ? <p className={cn('mb-0.5 font-semibold', titleTones[tone])}>{title}</p> : null}
        {children ? <div className="text-muted">{children}</div> : null}
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </div>
  );
}
