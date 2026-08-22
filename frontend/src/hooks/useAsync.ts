import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';
import { ApiError } from '../api/client';

interface Options {
  /** Shown when the failure is not an ApiError and so carries no server message. */
  fallbackError?: string;
}

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /** Re-run the task with the same inputs (a Refresh button, say). */
  reload: () => void;
  clearError: () => void;
}

/**
 * Run an async task and expose it as data / loading / error.
 *
 * Three screens need exactly this and each used to hand-roll it, including its
 * own cancellation idiom. The task receives an AbortSignal that is tripped when
 * the inputs change or the component unmounts, so a slow response for the file
 * you just navigated away from can never overwrite the one you are looking at.
 *
 * `data` deliberately survives a re-run. A caller that wants a spinner instead
 * of stale content checks `loading`; one that wants to keep the old page on
 * screen while the next loads checks `loading && !data`.
 */
export function useAsync<T>(
  task: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  { fallbackError = 'Something went wrong' }: Options = {},
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  // The task closes over props, so it changes identity every render. Keeping it
  // in a ref lets `deps` alone decide when to re-run, instead of every render.
  const taskRef = useRef(task);
  useEffect(() => {
    taskRef.current = task;
  });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setLoading(true);
    setError(null);

    taskRef.current(controller.signal).then(
      (value) => {
        if (!active) return;
        setData(value);
        setLoading(false);
      },
      (cause: unknown) => {
        if (!active || controller.signal.aborted) return;
        setError(cause instanceof ApiError ? cause.message : fallbackError);
        setLoading(false);
      },
    );

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((current) => current + 1), []);
  const clearError = useCallback(() => setError(null), []);

  return { data, loading, error, reload, clearError };
}
