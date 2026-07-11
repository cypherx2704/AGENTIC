'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useSession } from './SessionProvider';
import { ThemeToggle } from './ThemeToggle';
import { AgentNamesProvider } from './AgentNames';
import { Loading } from './ui';
import { config } from '@/lib/config';
import { cn } from '@/lib/utils';

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  /** When set, the item shows only if the session carries this scope (or platform:admin). */
  scope?: string;
}
interface NavGroup {
  label: string;
  items: NavItem[];
}

// Small, consistent 1.8px stroke icons. Structure encodes grouping, not decoration.
const I = {
  overview: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="3" y="3" width="7" height="8" rx="1" /><rect x="14" y="3" width="7" height="5" rx="1" /><rect x="14" y="11" width="7" height="10" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /></svg>
  ),
  agents: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="8" r="3.5" /><path d="M5 20c0-3.3 3.1-5.5 7-5.5s7 2.2 7 5.5" /></svg>
  ),
  orchestrator: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="6" cy="6" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="12" cy="18" r="2" /><path d="M8 7l3 9M16 7l-3 9" /></svg>
  ),
  runner: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M5 12h4l2-6 3 12 2-6h3" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  feed: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01" strokeLinecap="round" /></svg>
  ),
  approvals: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="9" /></svg>
  ),
  guardrails: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" /></svg>
  ),
  kb: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M5 4h13a1 1 0 0 1 1 1v14a2 2 0 0 0-2-2H5z" /></svg>
  ),
  memory: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></svg>
  ),
  tools: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8 7.2 20l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.2-.4-.4-2.2z" strokeLinejoin="round" /></svg>
  ),
  builder: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M8.2 6.6h7.6M7.4 7.7l3.4 8M16.6 7.7l-3.4 8" strokeLinecap="round" /></svg>
  ),
  skills: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M9 4a2 2 0 0 1 4 0v1h3a1 1 0 0 1 1 1v3h1a2 2 0 0 1 0 4h-1v3a1 1 0 0 1-1 1h-3v-1a2 2 0 0 0-4 0v1H6a1 1 0 0 1-1-1v-3H4a2 2 0 0 1 0-4h1V6a1 1 0 0 1 1-1h3z" strokeLinejoin="round" /></svg>
  ),
  playground: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-4 3v-3H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" strokeLinejoin="round" /><path d="M8 10l2 2-2 2M13 14h3" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  webhooks: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M9 7a3 3 0 1 1 4 2.8l2.2 3.9M15 12a3 3 0 1 1-2.4 4.8L8 17M9.5 12.2A3 3 0 1 1 7 17h4.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  admin: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3l7 3v5c0 4-3 7-7 9-4-2-7-5-7-9V6z" /><path d="M9.5 12l1.8 1.8L15 10" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  llm: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" rx="1" /></svg>
  ),
  aliases: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 7h16M4 12h16M4 17h10" /></svg>
  ),
  usage: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 19V5M4 19h16M8 15l3-4 3 2 4-6" /></svg>
  ),
  keys: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="8" cy="8" r="4" /><path d="M11 11l7 7-2 2-2-1-1-2-2-1z" /></svg>
  ),
  tenant: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 7l8-4 8 4-8 4z" /><path d="M4 7v6l8 4 8-4V7" /></svg>
  ),
  audit: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M9 5h6M8 5a2 2 0 0 0-2 2v12l3-2 2 2 2-2 3 2V7a2 2 0 0 0-2-2" /></svg>
  ),
  health: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M3 12h4l2 6 4-14 2 8h6" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
};

const NAV: NavGroup[] = [
  { label: 'Overview', items: [{ href: '/', label: 'Dashboard', icon: I.overview }] },
  {
    label: 'Agents',
    items: [
      { href: '/agents', label: 'Agents', icon: I.agents },
      { href: '/orchestrator', label: 'Orchestrator', icon: I.orchestrator },
    ],
  },
  {
    label: 'Operations',
    items: [
      { href: '/tasks/run', label: 'Task Runner', icon: I.runner },
      { href: '/tasks', label: 'Task Feed', icon: I.feed },
      { href: '/hil', label: 'Approvals', icon: I.approvals },
    ],
  },
  { label: 'Safety', items: [{ href: '/guardrails', label: 'Guardrails', icon: I.guardrails }] },
  {
    label: 'Knowledge',
    items: [
      { href: '/rag', label: 'Knowledge Bases', icon: I.kb },
      { href: '/memory', label: 'Memory', icon: I.memory },
    ],
  },
  {
    label: 'Capabilities',
    items: [
      { href: '/tools', label: 'Tools', icon: I.tools },
      { href: '/tools/builder', label: 'Tool Builder', icon: I.builder, scope: 'tool:admin' },
      { href: '/skills', label: 'Skills', icon: I.skills },
    ],
  },
  {
    label: 'Models',
    items: [
      { href: '/llms', label: 'LLM Connections', icon: I.llm },
      { href: '/llms/aliases', label: 'Aliases & Rules', icon: I.aliases },
      { href: '/llms/playground', label: 'Playground', icon: I.playground },
      { href: '/usage', label: 'Usage & Cost', icon: I.usage },
    ],
  },
  {
    label: 'Platform',
    items: [
      { href: '/keys', label: 'API Keys', icon: I.keys },
      { href: '/webhooks', label: 'Webhooks', icon: I.webhooks },
      { href: '/tenant', label: 'Tenant & Quotas', icon: I.tenant },
      { href: '/audit', label: 'Audit Log', icon: I.audit },
      { href: '/health', label: 'Platform Health', icon: I.health },
      { href: '/admin', label: 'Platform Admin', icon: I.admin, scope: 'platform:admin' },
    ],
  },
];

const ALL_ITEMS = NAV.flatMap((g) => g.items);

/**
 * Resolve the single active nav item by longest-prefix match, so a child route
 * (/tasks/run) lights up only its own item — never also its parent (/tasks).
 */
function activeNavHref(pathname: string): string {
  return ALL_ITEMS.reduce((best, item) => {
    const matches =
      item.href === '/'
        ? pathname === '/'
        : pathname === item.href || pathname.startsWith(`${item.href}/`);
    return matches && item.href.length > best.length ? item.href : best;
  }, '');
}

function NavList({
  pathname,
  scopes,
  onNavigate,
}: {
  pathname: string;
  scopes: readonly string[];
  onNavigate?: () => void;
}) {
  const activeHref = activeNavHref(pathname);
  const scopeSet = new Set(scopes);
  const canSee = (item: NavItem) =>
    !item.scope || scopeSet.has(item.scope) || scopeSet.has('platform:admin');
  return (
    <div className="flex flex-col gap-3">
      {NAV.map((group) => {
        const items = group.items.filter(canSee);
        if (items.length === 0) return null;
        return (
        <div key={group.label}>
          <p className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">{group.label}</p>
          <ul className="flex flex-col gap-px">
            {items.map((item) => {
              const active = item.href === activeHref;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? 'page' : undefined}
                    className={cn(
                      'relative flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm font-medium transition-colors',
                      active
                        ? "bg-surface-2 text-fg-strong before:absolute before:-left-2 before:top-1.5 before:bottom-1.5 before:w-0.5 before:rounded-full before:bg-brand before:content-['']"
                        : 'text-muted hover:bg-surface-2 hover:text-fg',
                    )}
                  >
                    <span className={cn('h-[15px] w-[15px] shrink-0', active ? 'text-fg' : '')}>{item.icon}</span>
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
        );
      })}
    </div>
  );
}

/**
 * The authenticated app shell: compact top bar + grouped side nav + the auth guard.
 * Redirects to /login (preserving the destination) when the BFF reports no session.
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

  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

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
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loading label="Redirecting to sign in…" />
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="z-30 flex h-12 shrink-0 items-center gap-3 border-b border-border bg-surface px-3">
        <button
          type="button"
          onClick={() => setDrawerOpen(true)}
          aria-label="Open navigation menu"
          aria-expanded={drawerOpen}
          className="-ml-0.5 grid h-8 w-8 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-fg md:hidden"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 6h18M3 12h18M3 18h18" strokeLinecap="round" /></svg>
        </button>
        <div className="flex items-center gap-2">
          <span className="grid h-[22px] w-[22px] place-items-center rounded bg-brand text-[13px] font-bold text-brand-fg">C</span>
          <span className="text-[15px] font-semibold tracking-tight text-fg-strong">{config.appName}</span>
        </div>
        <div className="flex-1" />
        <span className="hidden items-center gap-1.5 rounded-md border border-border px-2 py-1 font-mono text-xs text-muted sm:inline-flex" title="Environment">
          <span className="h-1.5 w-1.5 rounded-full bg-success" />prod
        </span>
        <ThemeToggle />
        <button
          onClick={() => void signOut().then(() => router.replace('/login'))}
          className="h-8 rounded-md border border-border-2 px-2.5 text-[13px] font-medium text-fg hover:bg-surface-2"
        >
          Sign Out
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <nav className="hidden w-[230px] shrink-0 flex-col overflow-hidden border-r border-border bg-surface md:flex" aria-label="Primary">
          <div className="min-h-0 flex-1 overflow-y-auto p-2.5">
            <NavList pathname={pathname} scopes={session.scopes ?? []} />
          </div>
          <div className="flex shrink-0 items-center gap-2 border-t border-border p-2.5">
            <span className="grid h-7 w-7 place-items-center rounded bg-brand text-[12px] font-bold text-brand-fg">
              {config.appName.slice(0, 1).toUpperCase()}
            </span>
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-fg">Orchestrator</p>
              <p className="truncate text-[11px] text-muted">Tenant console</p>
            </div>
          </div>
        </nav>

        <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-bg">
          <AgentNamesProvider>{children}</AgentNamesProvider>
        </main>
      </div>

      {drawerOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/50" onClick={() => setDrawerOpen(false)} aria-hidden="true" />
          <nav className="absolute left-0 top-0 flex h-full w-[248px] flex-col border-r border-border bg-surface" aria-label="Primary">
            <div className="flex h-12 items-center justify-between border-b border-border px-3">
              <div className="flex items-center gap-2">
                <span className="grid h-[22px] w-[22px] place-items-center rounded bg-brand text-[13px] font-bold text-brand-fg">C</span>
                <span className="text-[15px] font-semibold text-fg-strong">{config.appName}</span>
              </div>
              <button
                type="button"
                onClick={() => setDrawerOpen(false)}
                aria-label="Close navigation menu"
                className="grid h-7 w-7 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-fg"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6 6 18M6 6l12 12" strokeLinecap="round" /></svg>
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-2.5">
              <NavList pathname={pathname} scopes={session.scopes ?? []} onNavigate={() => setDrawerOpen(false)} />
            </div>
          </nav>
        </div>
      )}
    </div>
  );
}

/**
 * Page layout contract (fixed-header / hybrid-scroll shell):
 *   <Page>
 *     <PageHeader … />          ← pinned; never scrolls
 *     <PageBody>…</PageBody>    ← form/detail pages: this region scrolls as one
 *     — or —
 *     <PageBody fill>…</PageBody>  ← list/log pages: fills the viewport; scroll lives on
 *                                    an inner panel (e.g. a Card body with overflow-y-auto)
 *   </Page>
 * The shell's <main> is a fixed-height flex column (overflow-hidden), so every page MUST
 * wrap its content in <Page> — the header stays put and the browser window never scrolls.
 */
export function Page({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex h-full min-h-0 flex-col', className)}>{children}</div>;
}

/** The scrollable body region under a pinned PageHeader. `fill` = don't scroll here; an inner panel does. */
export function PageBody({
  children,
  fill = false,
  className,
}: {
  children: ReactNode;
  fill?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'min-h-0 flex-1 px-5 py-4',
        fill ? 'flex flex-col overflow-hidden' : 'overflow-y-auto',
        className,
      )}
    >
      {children}
    </div>
  );
}

/**
 * Standard page header — a pinned bar at the top of every screen (first child of <Page>). It shares
 * the body's horizontal padding (px-5) so the title never hugs the sidebar and trailing actions
 * (back-links etc.) never run off the right edge; a hairline border delineates it from the scroll body.
 */
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
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-5 py-3.5">
      <div className="min-w-0">
        <h1 className="truncate text-[19px] font-semibold tracking-tight text-fg-strong">{title}</h1>
        {description && <div className="mt-0.5 text-[13px] text-muted">{description}</div>}
      </div>
      {actions && <div className="flex flex-shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
