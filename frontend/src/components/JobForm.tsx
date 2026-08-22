import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type { CreateJobPayload, OperationInfo, OperationName, S3File } from '../api/types';
import { useAsync } from '../hooks/useAsync';
import { Empty, ErrorBanner, Spinner } from './ui';

interface Props {
  file: S3File;
  submitting: boolean;
  onSubmit: (payload: CreateJobPayload) => void;
}

const EXAMPLES: Record<OperationName, string> = {
  REPLACE: 'Find email addresses and replace them',
  MASK: 'Mask email addresses but keep the first letter and the domain',
  EXTRACT: 'Extract the domain from each email address',
  VALIDATE: 'Flag rows where the email address is not valid',
};

const FALLBACK_OPERATIONS = [
  { value: 'REPLACE', label: 'Replace matches with a fixed value' } as OperationInfo,
];

// A column called "email" or "notes" is almost always the one you meant.
const LIKELY_TARGET = /mail|phone|note|body|text/i;

export function JobForm({ file, submitting, onSubmit }: Props) {
  const [operation, setOperation] = useState<OperationName>('REPLACE');
  const [description, setDescription] = useState('');
  const [replacement, setReplacement] = useState('REDACTED');
  const [columns, setColumns] = useState<string[]>([]);
  const [sheet, setSheet] = useState('');
  const [forceRefresh, setForceRefresh] = useState(false);

  // The operation catalogue is static, so it is fetched once; the preview is
  // re-read whenever the file or the selected sheet changes.
  const catalogue = useAsync((signal) => api.operations(signal), [], {
    fallbackError: 'Could not load the operation list',
  });
  const { data: preview, loading, error } = useAsync(
    (signal) => api.preview(file.key, sheet || undefined, signal),
    [file.key, sheet],
    { fallbackError: 'Could not read that file' },
  );

  const operations = catalogue.data?.operations ?? [];
  const isExcel = file.extension === '.xlsx' || file.extension === '.xls';

  // Pre-select a likely target so the common case is one click.
  useEffect(() => {
    if (!preview) return;
    const guess = preview.columns.find((column) => LIKELY_TARGET.test(column));
    setColumns(guess ? [guess] : []);
  }, [preview]);

  const activeOperation = useMemo(
    () => operations.find((item) => item.value === operation),
    [operations, operation],
  );

  const needsReplacement = activeOperation?.needs_replacement ?? operation === 'REPLACE';
  const canSubmit = !submitting && columns.length > 0 && description.trim().length >= 3 && (!needsReplacement || replacement.length > 0);

  const toggleColumn = (column: string) => {
    setColumns((current) =>
      current.includes(column) ? current.filter((item) => item !== column) : [...current, column],
    );
  };

  const submit = () => {
    onSubmit({
      source_key: file.key,
      sheet_name: sheet || undefined,
      operation,
      natural_language: description.trim(),
      replacement_value: needsReplacement ? replacement : '',
      target_columns: columns,
      force_refresh: forceRefresh,
    });
  };

  if (loading || catalogue.loading) return <Spinner label={`Reading the header of ${file.name}…`} />;
  if (error) return <ErrorBanner title="Preview failed" message={error} />;
  if (!preview) return <Empty>Nothing to show.</Empty>;

  return (
    <div className="form">
      {isExcel && preview.sheet_names.length > 1 && (
        <label className="field">
          <span>Sheet</span>
          <select value={sheet} onChange={(event) => setSheet(event.target.value)}>
            <option value="">{preview.sheet_names[0]} (first)</option>
            {preview.sheet_names.slice(1).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="field">
        <span>Sample rows</span>
        <div className="table-scroll table-scroll-compact">
          <table className="data-table">
            <thead>
              <tr>
                {preview.columns.map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {preview.sample_rows.map((row, index) => (
                <tr key={index}>
                  {preview.columns.map((column) => (
                    <td key={column}>{row[column] ?? ''}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {preview.note && <p className="muted">{preview.note}</p>}
      </div>

      <fieldset className="field">
        <span>Target column(s)</span>
        <div className="chips">
          {preview.columns.map((column) => (
            <button
              key={column}
              type="button"
              className={`chip${columns.includes(column) ? ' chip-active' : ''}`}
              onClick={() => toggleColumn(column)}
            >
              {column}
            </button>
          ))}
        </div>
      </fieldset>

      <label className="field">
        <span>Operation</span>
        <select
          value={operation}
          onChange={(event) => setOperation(event.target.value as OperationName)}
        >
          {(operations.length ? operations : FALLBACK_OPERATIONS).map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span>Describe the pattern in plain English</span>
        <textarea
          rows={3}
          placeholder={EXAMPLES[operation]}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
        <button type="button" className="btn-link" onClick={() => setDescription(EXAMPLES[operation])}>
          Use the example
        </button>
      </label>

      {needsReplacement && (
        <label className="field">
          <span>Replacement value</span>
          <input
            type="text"
            value={replacement}
            onChange={(event) => setReplacement(event.target.value)}
            placeholder="REDACTED"
          />
        </label>
      )}

      {operation === 'MASK' && (
        <p className="hint">
          The model produces the mask template itself (for example <code>$1***$2</code>), so no
          replacement value is needed.
        </p>
      )}
      {operation === 'EXTRACT' && (
        <p className="hint">A new <code>&lt;column&gt;_extracted</code> column is added; originals are kept.</p>
      )}
      {operation === 'VALIDATE' && (
        <p className="hint">A boolean <code>&lt;column&gt;_valid</code> column is added; nothing is overwritten.</p>
      )}

      <label className="checkbox">
        <input
          type="checkbox"
          checked={forceRefresh}
          onChange={(event) => setForceRefresh(event.target.checked)}
        />
        <span>Bypass the prompt cache (ask the model again)</span>
      </label>

      <div className="row-between">
        <p className="muted">
          {columns.length === 0
            ? 'Pick at least one column.'
            : `${columns.length} column(s) selected: ${columns.join(', ')}`}
        </p>
        <button type="button" className="btn btn-primary" onClick={submit} disabled={!canSubmit}>
          {submitting ? 'Submitting…' : 'Run job'}
        </button>
      </div>
    </div>
  );
}
