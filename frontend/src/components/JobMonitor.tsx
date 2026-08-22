import type { Job } from '../api/types';
import { formatNumber } from '../lib/format';
import { StatusPill } from './StatusPill';
import { ErrorBanner, ProgressBar } from './ui';

interface Props {
  job: Job;
  pollError: string | null;
  cancelling: boolean;
  onCancel: () => void;
}

export function JobMonitor({ job, pollError, cancelling, onCancel }: Props) {
  const running = job.status === 'QUEUED' || job.status === 'RUNNING';
  const regex = job.regex;

  return (
    <div className="monitor">
      <div className="row-between">
        <div className="row-gap">
          <StatusPill status={job.status} />
          <code className="muted">{job.id.slice(0, 8)}</code>
          {job.attempt > 1 && <span className="tag">attempt {job.attempt}</span>}
        </div>
        {running && (
          <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={cancelling}>
            {cancelling ? 'Cancelling…' : 'Cancel job'}
          </button>
        )}
      </div>

      <ProgressBar value={job.progress} indeterminate={job.status === 'QUEUED'} />
      <div className="row-between monitor-phase">
        <span>{job.phase}</span>
        <span className="muted">{job.progress}%</span>
      </div>

      {pollError && <ErrorBanner title="Polling" message={pollError} />}

      {job.status === 'FAILED' && (
        <ErrorBanner title={job.error_type || 'Job failed'} message={job.error_message} />
      )}

      {job.status === 'CANCELLED' && (
        <div className="banner banner-warn">Cancelled. The Spark job was interrupted mid-flight.</div>
      )}

      {regex.pattern && (
        <div className="regex-card">
          <div className="row-between">
            <h3>Generated pattern</h3>
            <div className="row-gap">
              {regex.cached && <span className="tag tag-cache">from Redis cache</span>}
              {!regex.cached && regex.provider && <span className="tag">{regex.provider}</span>}
              {regex.self_test_passed === true && <span className="tag tag-ok">self-test passed</span>}
              {regex.self_test_passed === false && <span className="tag tag-warn">self-test failed</span>}
            </div>
          </div>
          <pre className="regex">{regex.pattern}</pre>
          {regex.replacement_template && (
            <p className="muted">
              Mask template: <code>{regex.replacement_template}</code>
            </p>
          )}
          {job.operation === 'EXTRACT' && (
            <p className="muted">Capture group: {regex.group}</p>
          )}
          {regex.explanation && <p className="explanation">{regex.explanation}</p>}
          <div className="row-gap muted small">
            {regex.model && <span>{regex.model}</span>}
            {regex.confidence > 0 && <span>confidence {(regex.confidence * 100).toFixed(0)}%</span>}
            {regex.case_insensitive && <span>case-insensitive</span>}
          </div>
          {regex.warnings.length > 0 && (
            <ul className="warnings">
              {regex.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <dl className="stats">
        <div>
          <dt>Rows processed</dt>
          <dd>{job.total_rows ? formatNumber(job.total_rows) : '—'}</dd>
        </div>
        <div>
          <dt>Cells affected</dt>
          <dd>{job.matched_cells ? formatNumber(job.matched_cells) : job.status === 'SUCCESS' ? '0' : '—'}</dd>
        </div>
        <div>
          <dt>Queue wait</dt>
          <dd>{job.queue_wait_seconds != null ? `${job.queue_wait_seconds}s` : '—'}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{job.duration_seconds != null ? `${job.duration_seconds}s` : '—'}</dd>
        </div>
      </dl>
    </div>
  );
}
