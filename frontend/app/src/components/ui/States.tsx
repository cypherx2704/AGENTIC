import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';
import { Spinner } from './Spinner';

export function EmptyState({ title, description, action }: { title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border px-6 py-12 text-center">
      <p className="text-sm font-medium text-fg">{title}</p>
      {description && <p className="mt-1 max-w-sm text-sm text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Loading({ label = 'Loading…', className }: { label?: string; className?: string }) {
  return (
    <div className={cn('flex items-center justify-center gap-3 py-12 text-muted', className)}>
      <Spinner />
      <span className="text-sm">{label}</span>
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded bg-surface-2', className)} />;
}
