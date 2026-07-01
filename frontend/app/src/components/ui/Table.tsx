import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T) => ReactNode;
  className?: string;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  empty,
}: {
  columns: Array<Column<T>>;
  rows: T[];
  rowKey: (row: T, i: number) => string;
  onRowClick?: (row: T) => void;
  empty?: ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left">
            {columns.map((c) => (
              <th key={c.key} className={cn('px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-muted', c.className)}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-4 py-10 text-center text-sm text-muted">
                {empty ?? 'No records.'}
              </td>
            </tr>
          ) : (
            rows.map((row, i) => (
              <tr
                key={rowKey(row, i)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn(
                  'border-b border-border/60 transition-colors',
                  onRowClick && 'cursor-pointer hover:bg-surface-2',
                )}
              >
                {columns.map((c) => (
                  <td key={c.key} className={cn('px-4 py-2.5 align-top text-fg', c.className)}>
                    {c.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
