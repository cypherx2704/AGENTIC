import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

/** Flat surface, thin border, no shadow — the console's primary container. */
export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn('rounded-md border border-border bg-surface', className)}>{children}</div>;
}

export function CardHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex items-center justify-between gap-3 border-b border-border px-4 py-2.5', className)}>
      <div className="min-w-0">
        <h3 className="truncate text-sm font-semibold text-fg-strong">{title}</h3>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn('px-4 py-3.5', className)}>{children}</div>;
}

/** A labeled metric/stat tile used across the console. Dense and flat. */
export function Stat({ label, value, sub }: { label: ReactNode; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-surface px-3.5 py-2.5">
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className="mt-1 text-[22px] font-semibold tabular-nums text-fg-strong">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
    </div>
  );
}
