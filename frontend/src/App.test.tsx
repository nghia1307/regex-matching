import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { OPERATIONS, makeFile, makeJob, makePreview, makeResultPage } from './test/factories';

// vi.mock is hoisted above the imports, so the stubs have to be hoisted too.
const mocks = vi.hoisted(() => ({
  health: vi.fn(),
  files: vi.fn(),
  preview: vi.fn(),
  operations: vi.fn(),
  createJob: vi.fn(),
  getJob: vi.fn(),
  listJobs: vi.fn(),
  cancelJob: vi.fn(),
  result: vi.fn(),
}));

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>();
  return { ...actual, api: mocks };
});

const JOB_ID = '11111111-2222-3333-4444-555555555555';

describe('App wizard', () => {
  beforeEach(() => {
    mocks.health.mockResolvedValue({ status: 'ok', checks: {} });
    mocks.files.mockResolvedValue({ bucket: 'datasets', prefix: '', count: 1, files: [makeFile()] });
    mocks.preview.mockResolvedValue(makePreview());
    mocks.operations.mockResolvedValue({ operations: OPERATIONS });
    mocks.createJob.mockResolvedValue(makeJob({ id: JOB_ID, status: 'QUEUED' }));
    mocks.listJobs.mockResolvedValue({ count: 0, next: null, previous: null, results: [] });
    mocks.result.mockResolvedValue(makeResultPage({ total_rows: 2, total_pages: 1, has_next: false }));
  });

  it('reveals each step only once the previous one is answered', async () => {
    const user = userEvent.setup();
    mocks.getJob
      .mockResolvedValueOnce(makeJob({ id: JOB_ID, status: 'RUNNING', progress: 60 }))
      .mockResolvedValue(makeJob({ id: JOB_ID, status: 'SUCCESS', progress: 100, finished_at: '2026-08-01T10:01:00Z' }));

    render(<App />);

    // Step 1 only.
    expect(await screen.findByText('Choose a file from the bucket')).toBeInTheDocument();
    expect(screen.queryByText('Describe the transformation')).not.toBeInTheDocument();

    await user.click(await screen.findByRole('button', { name: /customers\.csv/ }));

    // Step 2 appears; step 3 does not exist yet.
    expect(await screen.findByText('Describe the transformation')).toBeInTheDocument();
    expect(screen.queryByText('Job progress')).not.toBeInTheDocument();

    await screen.findByText('Sample rows');
    await user.type(screen.getByRole('textbox', { name: /Describe the pattern/ }), 'redact emails');
    await user.click(screen.getByRole('button', { name: 'Run job' }));

    // Step 3 shows the live job, then step 4 shows the data once it succeeds.
    expect(await screen.findByText('Job progress')).toBeInTheDocument();
    expect(await screen.findByText('Processed data', undefined, { timeout: 4000 })).toBeInTheDocument();
    // Two tables now: the form's sample rows and the processed result.
    expect(await screen.findAllByRole('table')).toHaveLength(2);
  });

  it('surfaces a submit failure without advancing the wizard', async () => {
    const user = userEvent.setup();
    const { ApiError } = await import('./api/client');
    mocks.createJob.mockRejectedValue(new ApiError(429, 'Throttled', 'Too many jobs queued'));

    render(<App />);
    await user.click(await screen.findByRole('button', { name: /customers\.csv/ }));
    await screen.findByText('Sample rows');
    await user.type(screen.getByRole('textbox', { name: /Describe the pattern/ }), 'redact emails');
    await user.click(screen.getByRole('button', { name: 'Run job' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Too many jobs queued');
    expect(screen.queryByText('Job progress')).not.toBeInTheDocument();
  });

  it('reports an unreachable API in the header instead of failing to render', async () => {
    mocks.health.mockRejectedValue(new Error('boom'));
    render(<App />);

    await waitFor(() => expect(screen.getByText('API: unreachable')).toBeInTheDocument());
  });
});
