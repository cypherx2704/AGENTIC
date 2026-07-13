'use client';

import { useEffect, useState } from 'react';

type Theme = 'light' | 'dark';

/** Resolve the theme actually in effect: an explicit data-theme wins, else the OS preference. */
function resolveTheme(): Theme {
  if (typeof document === 'undefined') return 'dark';
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'light' || attr === 'dark') return attr;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * Light/dark switch. Persists to localStorage and stamps data-theme on <html>, which the
 * token layer honours over the OS media query (see globals.css). A no-flash script in the
 * root layout applies the saved value before first paint.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    setTheme(resolveTheme());
  }, []);

  function toggle() {
    const next: Theme = resolveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem('cx-theme', next);
    } catch {
      /* private mode — the attribute still applies for this session */
    }
    setTheme(next);
  }

  const isDark = theme !== 'light';

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title="Toggle theme"
      className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted hover:border-border-2 hover:text-fg"
    >
      {/* Render only after mount so the icon matches the real theme (avoids SSR mismatch). */}
      {theme === null ? (
        <span className="h-[15px] w-[15px]" />
      ) : isDark ? (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="4" />
          <path
            d="M12 2v2M12 20v2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4"
            strokeLinecap="round"
          />
        </svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z" />
        </svg>
      )}
    </button>
  );
}
