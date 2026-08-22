import { api } from '../api/client';
import type { S3File } from '../api/types';
import { useAsync } from '../hooks/useAsync';
import { formatNumber } from '../lib/format';
import { Empty, ErrorBanner, Spinner } from './ui';

interface Props {
  selected: string | null;
  onSelect: (file: S3File) => void;
}

const ICONS: Record<string, string> = {
  '.csv': 'CSV',
  '.tsv': 'TSV',
  '.xlsx': 'XLSX',
  '.xls': 'XLS',
  '.parquet': 'PQT',
};

export function FileBrowser({ selected, onSelect }: Props) {
  const { data, loading, error, reload, clearError } = useAsync(
    (signal) => api.files(undefined, signal),
    [],
    { fallbackError: 'Could not list files' },
  );

  const files = data?.files ?? [];
  const bucket = data?.bucket ?? '';

  return (
    <>
      <div className="row-between">
        <p className="muted">
          {bucket ? (
            <>
              Bucket <code>{bucket}</code> &middot; {files.length} file{files.length === 1 ? '' : 's'}
            </>
          ) : (
            'Loading bucket…'
          )}
        </p>
        <button type="button" className="btn btn-ghost" onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>

      {error && <ErrorBanner message={error} onDismiss={clearError} />}
      {loading && <Spinner label="Listing objects…" />}

      {!loading && !error && files.length === 0 && (
        <Empty>
          No CSV, Excel or Parquet files found. Run <code>docker compose run --rm seed</code> to
          create the sample datasets.
        </Empty>
      )}

      {files.length > 0 && (
        <ul className="file-list">
          {files.map((file) => (
            <li key={file.key}>
              <button
                type="button"
                className={`file-row${selected === file.key ? ' file-row-active' : ''}`}
                onClick={() => onSelect(file)}
              >
                <span className="file-ext">{ICONS[file.extension] ?? file.extension.replace('.', '').toUpperCase()}</span>
                <span className="file-name">{file.name}</span>
                <span className="muted file-meta">{file.size_human}</span>
                {file.size > 50 * 1024 * 1024 && (
                  <span className="tag tag-scale">{formatNumber(Math.round(file.size / 1024 / 1024))} MB</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
