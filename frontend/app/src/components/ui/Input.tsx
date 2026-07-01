'use client';

import { forwardRef, useId, useState } from 'react';
import type { InputHTMLAttributes, TextareaHTMLAttributes, SelectHTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/utils';

const fieldBase =
  'w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-fg placeholder:text-muted ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-1 focus-visible:ring-offset-bg ' +
  'disabled:cursor-not-allowed disabled:opacity-50';

interface FieldWrapProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
}

export function Field({ label, hint, error, htmlFor, children }: FieldWrapProps) {
  return (
    <div className="flex flex-col gap-1.5">
      {label && (
        <label htmlFor={htmlFor} className="text-sm font-medium text-fg">
          {label}
        </label>
      )}
      {children}
      {error ? (
        <p className="text-xs text-danger">{error}</p>
      ) : hint ? (
        <p className="text-xs text-muted">{hint}</p>
      ) : null}
    </div>
  );
}

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, hint, error, className, id, ...rest },
  ref,
) {
  const autoId = useId();
  const fieldId = id ?? autoId;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={fieldId}>
      <input
        ref={ref}
        id={fieldId}
        className={cn(fieldBase, error ? 'border-danger focus-visible:ring-danger' : '', className)}
        aria-invalid={error ? true : undefined}
        {...rest}
      />
    </Field>
  );
});

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { label, hint, error, className, id, ...rest },
  ref,
) {
  const autoId = useId();
  const fieldId = id ?? autoId;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={fieldId}>
      <textarea
        ref={ref}
        id={fieldId}
        className={cn(fieldBase, 'min-h-[88px] resize-y font-mono', error ? 'border-danger' : '', className)}
        {...rest}
      />
    </Field>
  );
});

export interface PasswordInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
}

export const PasswordInput = forwardRef<HTMLInputElement, PasswordInputProps>(function PasswordInput(
  { label, hint, error, className, id, ...rest },
  ref,
) {
  const autoId = useId();
  const fieldId = id ?? autoId;
  const [visible, setVisible] = useState(false);

  return (
    <Field label={label} hint={hint} error={error} htmlFor={fieldId}>
      <div className="relative">
        <input
          ref={ref}
          id={fieldId}
          type={visible ? 'text' : 'password'}
          className={cn(
            fieldBase,
            'pr-10',
            error ? 'border-danger focus-visible:ring-danger' : '',
            className,
          )}
          aria-invalid={error ? true : undefined}
          {...rest}
        />
        <button
          type="button"
          onClick={() => setVisible((v) => !v)}
          className="absolute inset-y-0 right-0 flex items-center px-3 text-muted hover:text-fg focus:outline-none"
          aria-label={visible ? 'Hide password' : 'Show password'}
          tabIndex={-1}
        >
          {visible ? (
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
              <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
              <line x1="1" y1="1" x2="23" y2="23" />
            </svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          )}
        </button>
      </div>
    </Field>
  );
});

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { label, hint, error, className, id, children, ...rest },
  ref,
) {
  const autoId = useId();
  const fieldId = id ?? autoId;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={fieldId}>
      <select ref={ref} id={fieldId} className={cn(fieldBase, 'pr-8', className)} {...rest}>
        {children}
      </select>
    </Field>
  );
});
