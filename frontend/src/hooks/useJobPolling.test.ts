import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { makeJob } from '../test/factories';
import { useJobPolling } from './useJobPolling';

const getJob = vi.fn();

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: { getJob: (...args: unknown[]) => getJob(...args) } };
});

/** Fake timers plus React state updates need the advance wrapped in act(). */
const advance = (ms: number) => act(async () => {
  await vi.advanceTimersByTimeAsync(ms);
});

describe('useJobPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getJob.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('polls until the job reaches a terminal state, then stops', async () => {
    getJob
      .mockResolvedValueOnce(makeJob({ status: 'QUEUED', progress: 0 }))
      .mockResolvedValueOnce(makeJob({ status: 'RUNNING', progress: 50 }))
      .mockResolvedValueOnce(makeJob({ status: 'SUCCESS', progress: 100 }));

    const { result } = renderHook(() => useJobPolling('job-1'));

    await advance(0);
    expect(result.current.job?.status).toBe('QUEUED');

    await advance(1000);
    expect(result.current.job?.status).toBe('RUNNING');

    await advance(1000);
    expect(result.current.job?.status).toBe('SUCCESS');

    // No further requests once terminal, however long we wait.
    await advance(60_000);
    expect(getJob).toHaveBeenCalledTimes(3);
  });

  it('backs off from 1s to 5s as the job runs longer', async () => {
    getJob.mockResolvedValue(makeJob({ status: 'RUNNING' }));

    renderHook(() => useJobPolling('job-1'));
    await advance(0);
    expect(getJob).toHaveBeenCalledTimes(1);

    // First 10s: one call per second.
    await advance(10_000);
    expect(getJob).toHaveBeenCalledTimes(11);

    // Past a minute the interval is 5s, so 20s buys at most four more calls.
    await advance(60_000);
    const atOneMinute = getJob.mock.calls.length;
    await advance(20_000);
    expect(getJob.mock.calls.length - atOneMinute).toBeLessThanOrEqual(5);
  });

  it('survives transient failures and only surfaces an error after five in a row', async () => {
    getJob
      .mockRejectedValueOnce(new ApiError(0, 'NetworkError', 'down'))
      .mockResolvedValueOnce(makeJob({ status: 'RUNNING', progress: 70 }))
      .mockRejectedValue(new ApiError(0, 'NetworkError', 'down for good'));

    const { result } = renderHook(() => useJobPolling('job-1'));

    await advance(0);
    expect(result.current.error).toBeNull();

    await advance(1000);
    expect(result.current.job?.progress).toBe(70);
    expect(result.current.error).toBeNull();

    await advance(10_000);
    expect(result.current.error).toBe('down for good');
    // The last job we did see is still on screen: a flaky network must not blank the UI.
    expect(result.current.job?.progress).toBe(70);
  });

  it('does not poll and clears state when there is no job id', async () => {
    getJob.mockResolvedValue(makeJob({ status: 'RUNNING' }));

    const { result, rerender } = renderHook(({ id }: { id: string | null }) => useJobPolling(id), {
      initialProps: { id: 'job-1' as string | null },
    });

    await advance(0);
    expect(result.current.job).not.toBeNull();

    rerender({ id: null });
    expect(result.current.job).toBeNull();

    const callsSoFar = getJob.mock.calls.length;
    await advance(10_000);
    expect(getJob).toHaveBeenCalledTimes(callsSoFar);
  });
});
