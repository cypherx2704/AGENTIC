/** Tiny class-name combiner (no dependency). Filters falsy values + joins with spaces. */
export function cn(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ');
}

/** Format an ISO timestamp for compact display; returns the raw string on parse failure. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** Format a USD cost with enough precision for sub-cent LLM amounts. */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value === 0) return '$0.00';
  if (value < 0.01) return `$${value.toFixed(6)}`;
  return `$${value.toFixed(4)}`;
}

/** Format an integer token count with thousands separators. */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return value.toLocaleString();
}

/** Format a duration in milliseconds as a human string (e.g. 1,240 ms / 2.10 s). */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** Short-form a UUID/id for table cells (first segment + ellipsis). */
export function shortId(id: string | null | undefined, head = 8): string {
  if (!id) return '—';
  return id.length > head + 3 ? `${id.slice(0, head)}…` : id;
}
