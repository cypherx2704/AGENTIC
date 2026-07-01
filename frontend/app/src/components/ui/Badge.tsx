import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

const tones: Record<Tone, string> = {
  neutral: 'bg-surface-2 text-muted border-border',
  success: 'bg-success/15 text-success border-success/30',
  warning: 'bg-warning/15 text-warning border-warning/30',
  danger: 'bg-danger/15 text-danger border-danger/30',
  info: 'bg-brand/15 text-brand border-brand/30',
};

export function Badge({ tone = 'neutral', children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium',
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Map a status string to a sensible badge tone, then render it. */
export function StatusBadge({ status }: { status: string | null | undefined }) {
  const s = (status ?? '').toLowerCase();
  let tone: Tone = 'neutral';
  if (['completed', 'active', 'allow', 'ok', 'ready', 'healthy'].includes(s)) tone = 'success';
  else if (['running', 'pending', 'pending_config', 'warn', 'redact', 'building', 'ingesting'].includes(s)) tone = 'warning';
  else if (['failed', 'timeout', 'cancelled', 'block', 'revoked', 'error', 'down', 'inactive'].includes(s)) tone = 'danger';
  else if (['queued', 'draft'].includes(s)) tone = 'info';
  return <Badge tone={tone}>{status ?? '—'}</Badge>;
}
