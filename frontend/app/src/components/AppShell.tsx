'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useSession } from './SessionProvider';
import { Loading } from './ui';
import { config } from '@/lib/config';
import { cn } from '@/lib/utils';

interface NavItem {
  href: string;
  label: string;
}

const NAV: NavItem[] = [
  { href: '/', label: 'Dashboard' },
  { href: '/agents', label: 'Agents' },
  { href: '/orchestrator', label: 'Orchestrator' },
  { href: '/hil', label: 'Approvals' },
  { href: '/keys', label: 'API Keys' },
  { href: '/tasks/run', label: 'Task Runner' },
  { href: '/tasks', label: 'Task Feed' },
  { href: '/guardrails', label: 'Guardrails' },
  { href: '/llms', label: 'LLM Connections' },
  { href: '/llms/aliases', label: 'LLM Aliases & Rules' },
  { href: '/usage', label: 'LLM Usage' },
  { href: '/rag', label: 'Knowledge Bases' },
  { href: '/audit', label: 'Audit Log' },
  { href: '/tenant', label: 'Tenant' },
  { href: '/health', label: 'Platform Health' },
];

/**
 * Resolve the single active nav item for a path by longest-prefix match, so a child
 * route (/tasks/run) lights up only its own item — never also its parent (/tasks).
 * Returns '' when nothing matches.
 */
function activeNavHref(pathname: string): string {
  return NAV.reduce((best, item) => {
    const matches =
      item.href === '/'
        ? pathname === '/'
        : pathname === item.href || pathname.startsWith(`${item.href}/`);
    return matches && item.href.length > best.length ? item.href : best;
  }, '');
}

/** The nav link list, shared by the desktop sidebar and the mobile drawer. */
function NavList({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  const activeHref = activeNavHref(pathname);
  return (
    <ul className="flex flex-col gap-0.5">
      {NAV.map((item) => {
        const active = item.href === activeHref;
        return (
          <li key={item.href}>
            <Link
              href={item.href}
              onClick={onNavigate}
              aria-current={active ? 'page' : undefined}
              className={cn(
                'block rounded-md px-3 py-2 text-sm font-medium transition-colors',
                active ? 'bg-brand/15 text-brand' : 'text-muted hover:bg-surface-2 hover:text-fg',
              )}
            >
              {item.label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The authenticated app shell: top bar + side nav + the auth guard. When the BFF
 * reports the session is unauthenticated, it redirects to /login (preserving the
 * intended destination). Used by the `(app)` route group layout.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const { session, loading, signOut } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    if (!loading && session && !session.authenticated) {
      const next = encodeURIComponent(pathname || '/');
      router.replace(`/login?next=${next}`);
    }
  }, [loading, session, pathname, router]);

  // Close the mobile drawer whenever the route changes (a nav link was followed).
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  // Close the mobile drawer on Escape while it is open.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDrawerOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [drawerOpen]);

  if (loading || !session) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loading label="Checking session…" />
      </div>
    );
  }

  if (!session.authenticated) {
    // The effect above is redirecting; render nothing to avoid a flash of app chrome.
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loading label="Redirecting to sign in…" />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border bg-surface px-4">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation menu"
            aria-expanded={drawerOpen}
            className="-ml-1 rounded-md p-1.5 text-muted hover:bg-surface-2 hover:text-fg md:hidden"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M3 12h18M3 18h18" strokeLinecap="round" />
            </svg>
          </button>
          <span className="flex h-7 w-7 items-center justify-center rounded bg-brand text-sm font-bold text-brand-fg">
            C
          </span>
          <span className="text-sm font-semibold text-fg">{config.appName} Console</span>
        </div>
        <div className="flex items-center gap-3 text-xs text-muted">
          <span className="hidden font-mono sm:inline" title="Active tenant">
            tenant: {session.tenant_id ?? '—'}
          </span>
          <button
            onClick={() => void signOut().then(() => router.replace('/login'))}
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium text-fg hover:bg-surface-2"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="flex flex-1">
        <nav className="hidden w-56 shrink-0 border-r border-border bg-surface p-3 md:block" aria-label="Primary">
          <NavList pathname={pathname} />
        </nav>

        <main className="min-w-0 flex-1 bg-bg p-4 sm:p-6">
          <div className="mx-auto max-w-6xl">{children}</div>
        </main>
      </div>

      {/* Mobile off-canvas drawer — only mounted below the md breakpoint. */}
      {drawerOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setDrawerOpen(false)}
            aria-hidden="true"
          />
          <nav
            className="absolute left-0 top-0 flex h-full w-64 flex-col border-r border-border bg-surface shadow-xl"
            aria-label="Primary"
          >
            <div className="flex h-14 items-center justify-between border-b border-border px-4">
              <span className="text-sm font-semibold text-fg">{config.appName} Console</span>
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                aria-label="Close navigation menu"
                className="rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M18 6 6 18M6 6l12 12" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-3">
              <NavList pathname={pathname} onNavigate={() => setDrawerOpen(false)} />
            </div>
          </nav>
        </div>
      )}
    </div>
  );
}

/** Standard page header used at the top of every screen. */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold text-fg">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
