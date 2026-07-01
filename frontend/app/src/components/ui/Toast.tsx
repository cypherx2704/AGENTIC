'use client';

import { createContext, useCallback, useContext, useMemo, useReducer } from 'react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

type ToastTone = 'success' | 'error' | 'info' | 'warning';

interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastContextValue {
  push: (message: string, tone?: ToastTone) => void;
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

type Action = { type: 'add'; toast: Toast } | { type: 'remove'; id: number };

function reducer(state: Toast[], action: Action): Toast[] {
  switch (action.type) {
    case 'add':
      return [...state, action.toast];
    case 'remove':
      return state.filter((t) => t.id !== action.id);
    default:
      return state;
  }
}

let counter = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, dispatch] = useReducer(reducer, []);

  const push = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = ++counter;
    dispatch({ type: 'add', toast: { id, tone, message } });
    setTimeout(() => dispatch({ type: 'remove', id }), 5000);
  }, []);

  const value = useMemo<ToastContextValue>(
    () => ({
      push,
      success: (m) => push(m, 'success'),
      error: (m) => push(m, 'error'),
      info: (m) => push(m, 'info'),
    }),
    [push],
  );

  const toneClass: Record<ToastTone, string> = {
    success: 'border-success/40 bg-success/10 text-success',
    error: 'border-danger/40 bg-danger/10 text-danger',
    info: 'border-brand/40 bg-brand/10 text-brand',
    warning: 'border-warning/40 bg-warning/10 text-warning',
  };

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-full max-w-sm flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={cn(
              'pointer-events-auto flex items-start gap-2 rounded-md border bg-surface px-4 py-3 text-sm shadow-lg',
              toneClass[t.tone],
            )}
          >
            <span className="flex-1 break-words text-fg">{t.message}</span>
            <button
              onClick={() => dispatch({ type: 'remove', id: t.id })}
              className="text-muted hover:text-fg"
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used within a ToastProvider');
  return ctx;
}
