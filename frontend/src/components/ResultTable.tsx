import { useState } from 'react';
import { api } from '../api/client';
import { useAsync } from '../hooks/useAsync';
import { formatNumber } from '../lib/format';
import { Empty, ErrorBanner, Spinner } from './ui';

const PAGE_SIZES = [25, 50, 100, 250];

export function ResultTable({ jobId }: { jobId: string }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  const { data, loading, error } = useAsync(
    (signal) => api.result(jobId, page, pageSize, signal),
    [jobId, page, pageSize],
    { fallbackError: 'Could not load the page' },
  );

  // Only the very first load blanks the screen; paging keeps the old rows in
  // place so the table does not collapse and jump under the cursor.
  if (loading && !data) return <Spinner label="Reading the result…" />;
  if (error) return <ErrorBanner title="Result" message={error} />;
  if (!data) return <Empty>No result yet.</Empty>;
  if (data.total_rows === 0) {
    return <Empty>The job finished but produced no rows. Was the source file empty?</Empty>;
  }

  const added = new Set(data.added_columns);

  return (
    <div className="result">
      <div className="row-between">
        <p className="muted">
          {formatNumber(data.total_rows)} rows &middot; {formatNumber(data.matched_cells)} cells
          affected &middot; page {data.page} of {formatNumber(data.total_pages)}
        </p>
        <label className="inline-field">
          <span className="muted">Rows per page</span>
          <select
            value={pageSize}
            onChange={(event) => {
              setPageSize(Number(event.target.value));
              setPage(1);
            }}
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th className="row-num">#</th>
              {data.columns.map((column) => (
                <th key={column} className={added.has(column) ? 'col-added' : undefined}>
                  {column}
                  {added.has(column) && <span className="tag tag-new">new</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, index) => (
              <tr key={data.row_offset + index}>
                <td className="row-num">{formatNumber(data.row_offset + index + 1)}</td>
                {data.columns.map((column) => (
                  <td key={column} className={added.has(column) ? 'col-added' : undefined}>
                    {renderCell(row[column])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <button type="button" className="btn btn-ghost" onClick={() => setPage(1)} disabled={!data.has_previous}>
          « First
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setPage((current) => Math.max(1, current - 1))}
          disabled={!data.has_previous}
        >
          ‹ Previous
        </button>
        <span className="muted">
          {loading ? 'Loading…' : `${formatNumber(data.row_offset + 1)}–${formatNumber(data.row_offset + data.rows.length)}`}
        </span>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setPage((current) => current + 1)}
          disabled={!data.has_next}
        >
          Next ›
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setPage(data.total_pages)}
          disabled={!data.has_next}
        >
          Last »
        </button>
      </div>
    </div>
  );
}

function renderCell(value: string | number | boolean | null | undefined) {
  if (value === null || value === undefined || value === '') {
    return <span className="null">—</span>;
  }
  if (typeof value === 'boolean') {
    return <span className={value ? 'bool-true' : 'bool-false'}>{value ? 'valid' : 'invalid'}</span>;
  }
  return String(value);
}
