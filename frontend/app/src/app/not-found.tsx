import Link from 'next/link';

/**
 * App-wide 404 for any URL that matches no route (stale deep links, typos). Next.js
 * renders this for unmatched URLs across the whole app. It stands alone (outside the
 * authenticated shell) with its own full-screen layout, like /login.
 */
export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-bg px-4 text-center">
      <p className="font-mono text-sm text-muted">404</p>
      <h1 className="text-xl font-semibold text-fg">Page not found</h1>
      <p className="max-w-sm text-sm text-muted">
        The page you&rsquo;re looking for doesn&rsquo;t exist. It may have been moved, or the link
        is out of date.
      </p>
      <Link
        href="/"
        className="mt-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-brand-fg hover:opacity-90"
      >
        Go to the console
      </Link>
    </div>
  );
}
