import { api } from '../api/client';
import { useAsync } from '../hooks/useAsync';
import { formatNumber } from '../lib/format';
import { StatusPill } from './StatusPill';

interface Props {
  activeJobId: string | null;
  refreshToken: number;
  onOpen: (jobId: string) => void;
}

export function JobHistory({ activeJobId, refreshToken, onOpen }: Props) {
  // Errors are deliberately ignored: a failing history panel must not break the
  // main flow, so the aside simply stays empty.
  const { data } = useAsync(
    (signal) => api.listJobs(8, signal),
    [refreshToken, activeJobId],
  );
  const jobs = data?.results ?? [];

  if (jobs.length === 0) return null;

  return (
    <aside className="history">
      <h3>Recent jobs</h3>
      <ul>
        {jobs.map((job) => (
          <li key={job.id}>
            <button
              type="button"
              className={`history-row${job.id === activeJobId ? ' history-row-active' : ''}`}
              onClick={() => onOpen(job.id)}
            >
              <StatusPill status={job.status} />
              <span className="history-desc" title={job.natural_language}>
                {job.natural_language}
              </span>
              <span className="muted small">
                {job.operation} &middot; {job.total_rows ? `${formatNumber(job.total_rows)} rows` : '—'}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
