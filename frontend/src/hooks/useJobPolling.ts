import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api/client';
import type { Job } from '../api/types';

const TERMINAL = new Set(['SUCCESS', 'FAILED', 'CANCELLED']);

/**
 * Poll one job until it reaches a terminal state.
 *
 * The interval backs off (1s -> 5s) because a job that has already run for two
 * minutes is not going to finish in the next 900ms, and there is no reason to
 * keep hitting the API at full rate. Transient network errors are tolerated: the
 * poll keeps trying and only gives up after several consecutive failures, so a
 * backend restart mid-job does not wipe the UI.
 */
export function useJobPolling(jobId: string | null) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const failures = useRef(0);
  const startedAt = useRef<number>(0);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!jobId) return null;
    const fresh = await api.getJob(jobId, signal);
    setJob(fresh);
    return fresh;
  }, [jobId]);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setError(null);
      return;
    }

    let cancelled = false;
    let timer: number | undefined;
    const controller = new AbortController();
    failures.current = 0;
    startedAt.current = Date.now();
    setError(null);

    const tick = async () => {
      if (cancelled) return;
      try {
        const fresh = await refresh(controller.signal);
        failures.current = 0;
        if (fresh && TERMINAL.has(fresh.status)) return;
      } catch (cause) {
        if (cancelled || controller.signal.aborted) return;
        failures.current += 1;
        if (failures.current >= 5) {
          setError(cause instanceof ApiError ? cause.message : 'Lost contact with the API');
          return;
        }
      }
      const elapsed = Date.now() - startedAt.current;
      const delay = elapsed < 10_000 ? 1000 : elapsed < 60_000 ? 2000 : 5000;
      timer = window.setTimeout(tick, delay);
    };

    void tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId, refresh]);

  return { job, error, refresh };
}
