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

/** Acronyms/compounds that should not be naively Title-Cased word-by-word. */
const STATUS_OVERRIDES: Record<string, string> = {
  ok: 'OK',
  llm: 'LLM',
  api: 'API',
  kb: 'KB',
  pii: 'PII',
  human_in_loop: 'Human-in-Loop',
};

/** Humanize a wire status ('pending_config' → 'Pending Config', 'ok' → 'OK') for chip display. */
export function humanizeStatus(status: string | null | undefined): string {
  const raw = (status ?? '').trim();
  if (!raw) return '—';
  const key = raw.toLowerCase();
  if (STATUS_OVERRIDES[key]) return STATUS_OVERRIDES[key];
  return raw
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((w) => STATUS_OVERRIDES[w.toLowerCase()] ?? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ');
}

/** Map a status string to a sensible badge tone, then render it Title-Cased. */
export function StatusBadge({ status }: { status: string | null | undefined }) {
  const s = (status ?? '').toLowerCase();
  let tone: Tone = 'neutral';
  if (['completed', 'active', 'allow', 'ok', 'ready', 'healthy', 'passed', 'succeeded', 'done', 'granted', 'delivered'].includes(s))
    tone = 'success';
  else if (
    ['running', 'pending', 'pending_config', 'warn', 'redact', 'building', 'ingesting', 'processing', 'streaming', 'paused', 'retiring', 'rotating'].includes(s)
  )
    tone = 'warning';
  else if (
    ['failed', 'timeout', 'cancelled', 'block', 'blocked', 'revoked', 'error', 'down', 'inactive', 'denied', 'expired', 'disabled', 'retired'].includes(s)
  )
    tone = 'danger';
  else if (['queued', 'draft', 'test', 'ask', 'next'].includes(s)) tone = 'info';
  return <Badge tone={tone}>{humanizeStatus(status)}</Badge>;
}
