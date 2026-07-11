'use client';

import type { ReactNode } from 'react';
import { useId } from 'react';
import { cn } from '@/lib/utils';

export interface SwitchProps {
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  label?: ReactNode;
  hint?: ReactNode;
  id?: string;
  /** Accessible name for the control-only variant (when no string `label` is rendered). */
  ariaLabel?: string;
}

/**
 * A restrained on/off switch. On = accent track + white knob; off = neutral track + faint knob.
 * When `label`/`hint` are supplied it renders a full row; the text is clickable too (no nested
 * <label> so the control never double-toggles).
 */
export function Switch({ checked, onChange, disabled, label, hint, id, ariaLabel }: SwitchProps) {
  const autoId = useId();
  const fieldId = id ?? autoId;

  const control = (
    <button
      type="button"
      role="switch"
      id={fieldId}
      aria-checked={checked}
      aria-label={ariaLabel ?? (typeof label === 'string' ? label : undefined)}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2 focus-visible:ring-offset-bg',
        checked ? 'border-brand bg-brand' : 'border-border-2 bg-surface-2',
        disabled && 'cursor-not-allowed opacity-50',
      )}
    >
      <span
        className={cn(
          'inline-block h-3.5 w-3.5 transform rounded-full transition-transform',
          checked ? 'translate-x-[18px] bg-white' : 'translate-x-[3px] bg-faint',
        )}
      />
    </button>
  );

  if (!label && !hint) return control;

  return (
    <div className={cn('flex items-start gap-3', disabled && 'opacity-70')}>
      {control}
      <span
        className={cn('min-w-0 select-none', disabled ? 'cursor-not-allowed' : 'cursor-pointer')}
        onClick={() => !disabled && onChange(!checked)}
      >
        {label && <span className="block text-sm font-medium text-fg">{label}</span>}
        {hint && <span className="mt-0.5 block text-xs text-muted">{hint}</span>}
      </span>
    </div>
  );
}
