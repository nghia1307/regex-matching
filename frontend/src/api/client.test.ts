import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from './client';

function respond(body: unknown, init: { status?: number; text?: string } = {}) {
  const status = init.status ?? 200;
  const text = init.text ?? (body === undefined ? '' : JSON.stringify(body));
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
  } as Response);
}

describe('api client', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('unwraps the backend error envelope into an ApiError', async () => {
    fetchMock.mockReturnValue(
      respond({ error: { type: 'RegexTooComplex', message: 'Pattern rejected', detail: { steps: 9000 } } }, { status: 422 }),
    );

    const failure = await api.createJob({
      source_key: 'raw/customers.csv',
      operation: 'REPLACE',
      natural_language: 'redact emails',
      target_columns: ['email'],
    }).catch((cause: unknown) => cause);

    expect(failure).toBeInstanceOf(ApiError);
    const error = failure as ApiError;
    expect(error.status).toBe(422);
    expect(error.type).toBe('RegexTooComplex');
    expect(error.message).toBe('Pattern rejected');
    expect(error.detail).toEqual({ steps: 9000 });
  });

  it('falls back to a readable message when the body is not the expected envelope', async () => {
    fetchMock.mockReturnValue(respond(undefined, { status: 502, text: '<html>Bad Gateway</html>' }));

    const error = (await api.health().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.type).toBe('ParseError');
    expect(error.message).toContain('Bad Gateway');
  });

  it('reports an unreachable backend rather than leaking the fetch rejection', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    const error = (await api.health().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.type).toBe('NetworkError');
  });

  it('omits empty query parameters instead of sending blanks', async () => {
    fetchMock.mockReturnValue(respond({ key: 'k', columns: [], sample_rows: [] }));

    await api.preview('raw/customers.csv', undefined);

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/files/preview/?key=raw%2Fcustomers.csv',
      expect.objectContaining({ headers: { 'Content-Type': 'application/json' } }),
    );
  });

  it('treats 204 as an empty success rather than trying to parse a body', async () => {
    fetchMock.mockReturnValue(respond(undefined, { status: 204 }));

    await expect(api.cancelJob('abc')).resolves.toBeUndefined();
  });
});
