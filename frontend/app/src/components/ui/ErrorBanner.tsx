import type { ReactNode } from 'react';
import { BffError } from '@/lib/bff-client';
import { cn } from '@/lib/utils';

/**
 * Render any error consistently. A BffError shows its Contract-2 envelope (code +
 * message + trace_id); a plain Error shows its message. Tone defaults to danger but a
 * guardrail 422 is surfaced as a warning "blocked" banner.
 */
export function ErrorBanner({
  error,
  className,
  title,
}: {
  error: unknown;
  className?: string;
  title?: ReactNode;
}) {
  if (!error) return null;

  let code: string | undefined;
  let message: string;
  let traceId: string | undefined;
  let requestId: string | undefined;
  let tone: 'danger' | 'warning' = 'danger';

  if (error instanceof BffError) {
    code = error.code;
    message = error.message;
    traceId = error.traceId;
    requestId = error.requestId;
    if (error.isGuardrailViolation) tone = 'warning';
  } else if (error instanceof Error) {
    message = error.message;
  } else {
    message = String(error);
  }

  const toneClass =
    tone === 'warning'
      ? 'border-warning/40 bg-warning/10 text-warning'
      : 'border-danger/40 bg-danger/10 text-danger';

  return (
    <div className={cn('rounded-md border px-4 py-3 text-sm', toneClass, className)} role="alert">
      <div className="flex items-center gap-2">
        {code && (
          <span className="rounded bg-current/10 px-1.5 py-0.5 font-mono text-xs font-semibold">{code}</span>
        )}
        <span className="font-medium text-fg">{title ?? message}</span>
      </div>
      {title && <p className="mt-1 text-fg/90">{message}</p>}
      {(traceId || requestId) && (
        <p className="mt-1.5 font-mono text-xs text-muted">
          {traceId && <>trace: {traceId} </>}
          {requestId && <>· request: {requestId}</>}
        </p>
      )}
    </div>
  );
}
