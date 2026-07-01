'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { fetchSession, logout as bffLogout, setUnauthorizedHandler } from '@/lib/bff-client';
import { useToast } from '@/components/ui';
import type { Session } from '@/lib/types';

const UNAUTHENTICATED: Session = { authenticated: false, tenant_id: null, scopes: [], csrf_token: null };

interface SessionState {
  session: Session | null;
  loading: boolean;
  error: unknown;
  refresh: () => Promise<Session | null>;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<SessionState | null>(null);

/**
 * Holds the BFF session (`/bff/me`). Loads once on mount and exposes refresh/signOut.
 * The auth guard reads `session.authenticated` to gate the app shell.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  // Fire the "session expired" handling at most once per live session, so a burst of
  // concurrent 401s doesn't stack toasts or thrash the redirect.
  const expiredRef = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchSession();
      expiredRef.current = false; // a fresh session re-arms the expiry handler
      setSession(next);
      return next;
    } catch (err) {
      setError(err);
      setSession(UNAUTHENTICATED);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  const signOut = useCallback(async () => {
    try {
      await bffLogout();
    } finally {
      expiredRef.current = true; // intentional sign-out: don't also fire the expiry toast
      setSession(UNAUTHENTICATED);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Subscribe to the global 401 interceptor: when a session lapses mid-use, drop to
  // unauthenticated (the shell then redirects to /login?next=…) and tell the user why.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (expiredRef.current) return;
      expiredRef.current = true;
      toast.error('Your session expired. Please sign in again.');
      setSession(UNAUTHENTICATED);
    });
    return () => setUnauthorizedHandler(null);
  }, [toast]);

  const value = useMemo<SessionState>(
    () => ({ session, loading, error, refresh, signOut }),
    [session, loading, error, refresh, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error('useSession must be used within a SessionProvider');
  return ctx;
}
