import type {
  CreateJobPayload,
  FileListResponse,
  FilePreview,
  Job,
  JobSummary,
  OperationInfo,
  Paginated,
  ResultPage,
} from './types';

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api';

/** Every API failure arrives as this, so callers have one thing to catch. */
export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly detail?: unknown;

  constructor(status: number, type: string, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.type = type;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });
  } catch (cause) {
    // An aborted request is the caller changing its mind, not a failure to report.
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(0, 'NetworkError', 'Cannot reach the API. Is the backend running?', cause);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  const body = text ? safeJson(text) : null;

  if (!response.ok) {
    const error = (body as { error?: { type?: string; message?: string; detail?: unknown } })?.error;
    throw new ApiError(
      response.status,
      error?.type ?? 'HttpError',
      error?.message ?? `Request failed with status ${response.status}`,
      error?.detail,
    );
  }
  return body as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { error: { type: 'ParseError', message: text.slice(0, 300) } };
  }
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

// Every read takes an optional AbortSignal so useAsync can drop a response the
// user has already navigated away from.
export const api = {
  health: () => request<{ status: string; checks: Record<string, unknown> }>('/health/'),

  metrics: () => request<Record<string, unknown>>('/metrics/'),

  operations: (signal?: AbortSignal) => request<{ operations: OperationInfo[] }>('/operations/', { signal }),

  files: (prefix?: string, signal?: AbortSignal) =>
    request<FileListResponse>(`/files/${query({ prefix })}`, { signal }),

  preview: (key: string, sheet?: string, signal?: AbortSignal) =>
    request<FilePreview>(`/files/preview/${query({ key, sheet })}`, { signal }),

  createJob: (payload: CreateJobPayload) =>
    request<Job>('/jobs/', { method: 'POST', body: JSON.stringify(payload) }),

  getJob: (id: string, signal?: AbortSignal) => request<Job>(`/jobs/${id}/`, { signal }),

  listJobs: (limit = 10, signal?: AbortSignal) =>
    request<Paginated<JobSummary>>(`/jobs/${query({ page_size: limit })}`, { signal }),

  cancelJob: (id: string) => request<Job>(`/jobs/${id}/cancel/`, { method: 'POST' }),

  result: (id: string, page: number, pageSize: number, signal?: AbortSignal) =>
    request<ResultPage>(`/jobs/${id}/result/${query({ page, page_size: pageSize })}`, { signal }),
};
