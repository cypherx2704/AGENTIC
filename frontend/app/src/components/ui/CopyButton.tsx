'use client';

import { useEffect, useRef, useState } from 'react';
import { cn } from '@/lib/utils';

/**
 * Copy an opaque value (a UUID, a secret, a trace id) to the clipboard WITHOUT rendering it.
 * This is the sanctioned way to make an internal id available for support/debugging while
 * honoring the "never display a UUID" rule — the value only ever lives on the clipboard.
 */
export function CopyButton({
  value,
  label = 'Copy ID',
  copiedLabel = 'Copied',
  className,
}: {
  value: string;
  label?: string;
  copiedLabel?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard unavailable (insecure context) — silently no-op */
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      title={label}
      className={cn(
        'inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] font-medium text-muted transition-colors hover:bg-surface-2 hover:text-fg',
        className,
      )}
    >
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="9" y="9" width="11" height="11" rx="2" />
        <path d="M5 15V5a2 2 0 0 1 2-2h10" />
      </svg>
      {copied ? copiedLabel : label}
    </button>
  );
}
