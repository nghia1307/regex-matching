import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OPERATIONS, makeFile, makePreview } from '../test/factories';
import { JobForm } from './JobForm';

const preview = vi.fn();
const operations = vi.fn();

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return {
    ...actual,
    api: {
      preview: (...args: unknown[]) => preview(...args),
      operations: (...args: unknown[]) => operations(...args),
    },
  };
});

async function renderForm(onSubmit = vi.fn()) {
  const user = userEvent.setup();
  render(<JobForm file={makeFile()} submitting={false} onSubmit={onSubmit} />);
  await screen.findByText('Sample rows');
  return { user, onSubmit };
}

describe('JobForm', () => {
  beforeEach(() => {
    preview.mockResolvedValue(makePreview());
    operations.mockResolvedValue({ operations: OPERATIONS });
  });

  it('pre-selects the column most likely to hold the data', async () => {
    await renderForm();

    expect(screen.getByRole('button', { name: 'email' })).toHaveClass('chip-active');
    expect(screen.getByRole('button', { name: 'city' })).not.toHaveClass('chip-active');
    expect(screen.getByText(/1 column\(s\) selected: email/)).toBeInTheDocument();
  });

  it('keeps submit disabled until a column and a description are present', async () => {
    const { user } = await renderForm();
    const run = screen.getByRole('button', { name: 'Run job' });

    expect(run).toBeDisabled();

    await user.type(screen.getByRole('textbox', { name: /Describe the pattern/ }), 'redact emails');
    expect(run).toBeEnabled();

    // Deselecting the only column invalidates the form again.
    await user.click(screen.getByRole('button', { name: 'email' }));
    expect(run).toBeDisabled();
    expect(screen.getByText('Pick at least one column.')).toBeInTheDocument();
  });

  it('submits the payload the API expects', async () => {
    const { user, onSubmit } = await renderForm();

    await user.click(screen.getByRole('button', { name: 'city' }));
    await user.type(screen.getByRole('textbox', { name: /Describe the pattern/ }), '  redact emails  ');
    await user.clear(screen.getByRole('textbox', { name: 'Replacement value' }));
    await user.type(screen.getByRole('textbox', { name: 'Replacement value' }), 'HIDDEN');
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: 'Run job' }));

    expect(onSubmit).toHaveBeenCalledWith({
      source_key: 'raw/customers.csv',
      sheet_name: undefined,
      operation: 'REPLACE',
      natural_language: 'redact emails',
      replacement_value: 'HIDDEN',
      target_columns: ['email', 'city'],
      force_refresh: true,
    });
  });

  it('hides the replacement field for operations that do not need one', async () => {
    const { user, onSubmit } = await renderForm();

    expect(screen.getByRole('textbox', { name: 'Replacement value' })).toBeInTheDocument();

    await user.selectOptions(screen.getByRole('combobox', { name: 'Operation' }), 'MASK');

    expect(screen.queryByRole('textbox', { name: 'Replacement value' })).not.toBeInTheDocument();

    await user.type(screen.getByRole('textbox', { name: /Describe the pattern/ }), 'mask emails');
    await user.click(screen.getByRole('button', { name: 'Run job' }));

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ operation: 'MASK', replacement_value: '' }));
  });

  it('shows the failure instead of a broken form when the preview cannot be read', async () => {
    preview.mockRejectedValue(new (await import('../api/client')).ApiError(404, 'NotFound', 'Object is gone'));
    render(<JobForm file={makeFile()} submitting={false} onSubmit={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Object is gone'));
    expect(screen.queryByRole('button', { name: 'Run job' })).not.toBeInTheDocument();
  });
});
