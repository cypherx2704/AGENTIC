'use client';

import { useEffect } from 'react';
import { Button, Card, CardBody, ErrorBanner } from '@/components/ui';

/**
 * Route-segment error boundary for the authenticated console. Catches any uncaught
 * render/runtime error in an (app) page and renders a recoverable card *inside* the
 * shell chrome instead of blanking the whole SPA. `reset()` re-renders the segment.
 */
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface for local debugging; server-side errors are already logged by the BFF.
    console.error(error);
  }, [error]);

  return (
    <div className="mx-auto max-w-xl py-12">
      <Card>
        <CardBody className="flex flex-col gap-4">
          <div>
            <h1 className="text-lg font-semibold text-fg">Something went wrong</h1>
            <p className="mt-1 text-sm text-muted">
              This screen hit an unexpected error and couldn&rsquo;t render. You can retry, or head
              back to the dashboard.
            </p>
          </div>
          <ErrorBanner error={error} title="Unexpected error" />
          {error.digest && <p className="font-mono text-xs text-muted">ref: {error.digest}</p>}
          <div className="flex gap-2">
            <Button onClick={reset}>Try again</Button>
            <Button variant="secondary" onClick={() => window.location.assign('/')}>
              Back to dashboard
            </Button>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
