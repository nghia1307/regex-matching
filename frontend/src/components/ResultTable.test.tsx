import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { makeResultPage } from '../test/factories';
import { ResultTable } from './ResultTable';

const result = vi.fn();

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: { result: (...args: unknown[]) => result(...args) } };
});

const JOB_ID = 'job-1';

async function renderTable() {
  const user = userEvent.setup();
  render(<ResultTable jobId={JOB_ID} />);
  await screen.findByRole('table');
  return { user };
}

describe('ResultTable', () => {
  beforeEach(() => {
    result.mockResolvedValue(makeResultPage());
  });

  it('marks columns the job added and renders empty and boolean cells readably', async () => {
    await renderTable();

    const header = within(screen.getByRole('table')).getAllByRole('columnheader');
    expect(header.map((cell) => cell.textContent)).toEqual(['#', 'id', 'email', 'email_validnew']);

    const rows = within(screen.getByRole('table')).getAllByRole('row');
    expect(within(rows[1]).getByText('valid')).toBeInTheDocument();
    // Row 2 has an empty email and an invalid flag.
    expect(within(rows[2]).getByText('—')).toBeInTheDocument();
    expect(within(rows[2]).getByText('invalid')).toBeInTheDocument();
  });

  it('walks pages and asks the API for the page it is showing', async () => {
    const { user } = await renderTable();
    expect(result).toHaveBeenLastCalledWith(JOB_ID, 1, 50, expect.any(AbortSignal));
    expect(screen.getByRole('button', { name: '‹ Previous' })).toBeDisabled();

    result.mockResolvedValue(makeResultPage({ page: 2, row_offset: 50, has_previous: true }));
    await user.click(screen.getByRole('button', { name: 'Next ›' }));
    expect(result).toHaveBeenLastCalledWith(JOB_ID, 2, 50, expect.any(AbortSignal));

    result.mockResolvedValue(makeResultPage({ page: 3, row_offset: 100, has_previous: true, has_next: false }));
    await user.click(screen.getByRole('button', { name: 'Last »' }));
    expect(result).toHaveBeenLastCalledWith(JOB_ID, 3, 50, expect.any(AbortSignal));
    expect(await screen.findByRole('button', { name: 'Next ›' })).toBeDisabled();
  });

  it('returns to page one when the page size changes, so the offset stays valid', async () => {
    const { user } = await renderTable();

    result.mockResolvedValue(makeResultPage({ page: 2, row_offset: 50, has_previous: true }));
    await user.click(screen.getByRole('button', { name: 'Next ›' }));
    expect(result).toHaveBeenLastCalledWith(JOB_ID, 2, 50, expect.any(AbortSignal));

    await user.selectOptions(screen.getByRole('combobox', { name: /Rows per page/ }), '250');
    expect(result).toHaveBeenLastCalledWith(JOB_ID, 1, 250, expect.any(AbortSignal));
  });

  it('explains an empty result instead of rendering a headerless table', async () => {
    result.mockResolvedValue(makeResultPage({ total_rows: 0, rows: [] }));
    render(<ResultTable jobId={JOB_ID} />);

    expect(await screen.findByText(/finished but produced no rows/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});
