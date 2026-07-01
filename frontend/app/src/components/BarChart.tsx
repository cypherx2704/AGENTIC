'use client';

import { cn } from '@/lib/utils';

export interface BarDatum {
  label: string;
  value: number;
  /** Optional sub-segments rendered as a stacked bar (e.g. token cache breakdown). */
  segments?: Array<{ label: string; value: number; tone: string }>;
}

/**
 * Dependency-free horizontal bar chart (pure SVG/divs). Used by the usage/cost
 * dashboards so we don't pull a charting library into the bundle.
 */
export function BarChart({
  data,
  valueFormat = (v) => String(v),
  className,
}: {
  data: BarDatum[];
  valueFormat?: (v: number) => string;
  className?: string;
}) {
  const max = Math.max(1, ...data.map((d) => d.value));

  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-muted">No data for this window.</p>;
  }

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      {data.map((d) => {
        const pct = (d.value / max) * 100;
        return (
          <div key={d.label} className="flex items-center gap-3">
            <span className="w-36 shrink-0 truncate text-xs font-medium text-muted" title={d.label}>
              {d.label}
            </span>
            <div className="relative h-5 flex-1 overflow-hidden rounded bg-surface-2">
              {d.segments ? (
                <div className="flex h-full" style={{ width: `${pct}%` }}>
                  {d.segments.map((s, i) => {
                    const segPct = d.value > 0 ? (s.value / d.value) * 100 : 0;
                    return (
                      <div
                        key={i}
                        className={s.tone}
                        style={{ width: `${segPct}%` }}
                        title={`${s.label}: ${valueFormat(s.value)}`}
                      />
                    );
                  })}
                </div>
              ) : (
                <div className="h-full rounded bg-brand" style={{ width: `${pct}%` }} />
              )}
            </div>
            <span className="w-24 shrink-0 text-right font-mono text-xs text-fg">{valueFormat(d.value)}</span>
          </div>
        );
      })}
    </div>
  );
}
