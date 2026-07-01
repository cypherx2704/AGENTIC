'use client';

/**
 * Last-resort error boundary for failures in the ROOT layout itself (where the normal
 * (app)/error.tsx can't help). It must render its own <html>/<body>, and because the
 * root layout — and therefore globals.css/Tailwind — may not have mounted, it uses
 * inline styles so the fallback always renders.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          fontFamily: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif',
          background: '#0b0e14',
          color: '#e6e6e6',
        }}
      >
        <div
          style={{
            minHeight: '100vh',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 24,
          }}
        >
          <div style={{ maxWidth: 440, textAlign: 'center' }}>
            <h1 style={{ fontSize: 20, marginBottom: 8 }}>The console failed to load</h1>
            <p style={{ fontSize: 14, lineHeight: 1.5, opacity: 0.8, marginBottom: 16 }}>
              A critical error stopped the app from rendering. Reloading usually fixes it; if it
              persists, contact your platform administrator.
            </p>
            {error.digest && (
              <p style={{ fontFamily: 'monospace', fontSize: 12, opacity: 0.6, marginBottom: 16 }}>
                ref: {error.digest}
              </p>
            )}
            <button
              onClick={reset}
              style={{
                padding: '8px 16px',
                borderRadius: 6,
                border: '1px solid #2a2f3a',
                background: '#1a1f2a',
                color: '#e6e6e6',
                fontSize: 14,
                cursor: 'pointer',
              }}
            >
              Try again
            </button>
          </div>
        </div>
      </body>
    </html>
  );
}
