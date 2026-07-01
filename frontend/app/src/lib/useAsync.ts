'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: unknown;
  /** Re-run the loader (e.g. after a mutation or for polling). */
  reload: () => void;
  setData: (data: T | null) => void;
}

/**
 * Run an async loader on mount (and on dependency change), with abort-on-unmount.
 * The loader receives an AbortSignal so in-flight requests cancel cleanly.
 */
export function useAsync<T>(loader: (signal: AbortSignal) => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(null);
    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (active) {
          setData(result);
          setError(null);
        }
      })
      .catch((err) => {
        if (active && !(err instanceof DOMException && err.name === 'AbortError')) {
          setError(err);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  return { data, loading, error, reload, setData };
}
