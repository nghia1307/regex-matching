import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { useAsync } from './useAsync';

describe('useAsync', () => {
  it('moves from loading to data and reports nothing as an error', async () => {
    const task = vi.fn().mockResolvedValue('done');
    const { result } = renderHook(() => useAsync(task, []));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBe('done');
    expect(result.current.error).toBeNull();
    expect(task).toHaveBeenCalledTimes(1);
  });

  it('prefers the server message and falls back for anything else', async () => {
    const apiFailure = renderHook(() =>
      useAsync(() => Promise.reject(new ApiError(500, 'Boom', 'Spark died')), [], { fallbackError: 'nope' }),
    );
    await waitFor(() => expect(apiFailure.result.current.error).toBe('Spark died'));

    const plainFailure = renderHook(() =>
      useAsync(() => Promise.reject(new TypeError('undefined is not a function')), [], { fallbackError: 'nope' }),
    );
    await waitFor(() => expect(plainFailure.result.current.error).toBe('nope'));
  });

  it('re-runs on reload() and clears a stale error on the way', async () => {
    const task = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, 'NetworkError', 'offline'))
      .mockResolvedValue('recovered');

    const { result } = renderHook(() => useAsync(task, []));
    await waitFor(() => expect(result.current.error).toBe('offline'));

    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe('recovered'));
    expect(result.current.error).toBeNull();
    expect(task).toHaveBeenCalledTimes(2);
  });

  it('re-runs when deps change and aborts the request it no longer needs', async () => {
    const seen: AbortSignal[] = [];
    const task = vi.fn((signal: AbortSignal) => {
      seen.push(signal);
      return Promise.resolve(seen.length);
    });

    const { result, rerender } = renderHook(({ id }: { id: number }) => useAsync((signal) => task(signal), [id]), {
      initialProps: { id: 1 },
    });
    await waitFor(() => expect(result.current.data).toBe(1));

    rerender({ id: 2 });
    await waitFor(() => expect(result.current.data).toBe(2));

    expect(seen[0].aborted).toBe(true);
    expect(seen[1].aborted).toBe(false);
  });

  it('ignores a slow response that lost the race to a newer one', async () => {
    const resolvers: ((value: string) => void)[] = [];
    const task = () => new Promise<string>((resolve) => resolvers.push(resolve));

    const { result, rerender } = renderHook(({ id }: { id: number }) => useAsync(task, [id]), {
      initialProps: { id: 1 },
    });

    rerender({ id: 2 });
    await act(async () => {
      resolvers[1]('second');
      resolvers[0]('first (stale)');
    });

    expect(result.current.data).toBe('second');
  });

  it('lets the caller dismiss an error without re-running the task', async () => {
    const task = vi.fn().mockRejectedValue(new ApiError(404, 'NotFound', 'gone'));
    const { result } = renderHook(() => useAsync(task, []));
    await waitFor(() => expect(result.current.error).toBe('gone'));

    act(() => result.current.clearError());
    expect(result.current.error).toBeNull();
    expect(task).toHaveBeenCalledTimes(1);
  });
});
